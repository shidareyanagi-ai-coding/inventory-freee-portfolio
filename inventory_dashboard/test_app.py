import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import app


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class InventoryAppTest(unittest.TestCase):
    def setUp(self):
        # DATABASE_URL が設定された環境でも、このテストは必ずローカル SQLite を使う。
        self._original_database_url = os.environ.pop("DATABASE_URL", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp.name, "test_inventory.db")
        app.init_db()
        # A-3: 業務ロジックは organization_id 必須。テスト用の自組織を作りデモ seed を入れる。
        with app.get_conn() as conn:
            self.org_id = app.create_organization(conn, "テスト組織")
            app.seed_organization(conn, self.org_id)

    def tearDown(self):
        app.DB_PATH = self.original_db_path
        self.tmp.cleanup()
        if self._original_database_url is not None:
            os.environ["DATABASE_URL"] = self._original_database_url

    def _first_product(self, conn):
        return conn.execute(
            "SELECT * FROM products WHERE organization_id = ? ORDER BY id LIMIT 1",
            (self.org_id,),
        ).fetchone()

    def test_import_sales_history_feeds_forecast(self):
        # A-9: 売上履歴CSV取込 → 予測が読む sales×movement として入り、需要として読める。
        from forecasting import data as fdata

        csv = (
            "date,sku,product_name,quantity,unit_price\n"
            "2026-03-01,SKU-REAL-1,実商品1,4,1200\n"
            "2026-03-02,SKU-REAL-1,実商品1,6,1200\n"
            "2026-03-05,SKU-REAL-2,実商品2,2,800\n"
        )
        with app.get_conn() as conn:
            summary = app.import_sales_history(conn, self.org_id, csv)
            prod = conn.execute(
                "SELECT id FROM products WHERE organization_id = ? AND sku = 'SKU-REAL-1'", (self.org_id,)
            ).fetchone()
            series = fdata.load_demand_series(conn, self.org_id, prod["id"])
        self.assertEqual(summary["imported"], 3)
        self.assertEqual(summary["created_products"], 2)
        self.assertEqual(summary["skipped"], 0)
        self.assertEqual(float(series.sum()), 10.0)  # 4 + 6 が需要として読める

    def test_import_sales_history_skips_invalid_rows(self):
        csv = (
            "date,sku,product_name,quantity,unit_price\n"
            "2026-03-01,SKU-X,商品X,3,100\n"     # ok
            "bad-date,SKU-X,,1,100\n"            # 日付不正
            "2026-03-02,,商品Y,1,100\n"          # sku 空
            "2026-03-03,SKU-X,商品X,-2,100\n"    # 数量不正
        )
        with app.get_conn() as conn:
            summary = app.import_sales_history(conn, self.org_id, csv)
        self.assertEqual(summary["imported"], 1)
        self.assertEqual(summary["skipped"], 3)
        self.assertEqual(len(summary["errors"]), 3)

    def test_import_sales_history_does_not_touch_ledger(self):
        # Phase D(⑤): CSV取込は demand_history にだけ入る。sales/inventory_movements/freee には書かない。
        csv = (
            "date,sku,product_name,quantity,unit_price\n"
            "2026-03-01,SKU-DH,需要商品,4,1200\n"
            "2026-03-02,SKU-DH,需要商品,6,1200\n"
        )
        with app.get_conn() as conn:
            sales_before = conn.execute(
                "SELECT COUNT(*) AS c FROM sales WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            moves_before = conn.execute(
                "SELECT COUNT(*) AS c FROM inventory_movements WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            queue_before = conn.execute(
                "SELECT COUNT(*) AS c FROM freee_sync_queue WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            summary = app.import_sales_history(conn, self.org_id, csv)
            sales_after = conn.execute(
                "SELECT COUNT(*) AS c FROM sales WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            moves_after = conn.execute(
                "SELECT COUNT(*) AS c FROM inventory_movements WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            queue_after = conn.execute(
                "SELECT COUNT(*) AS c FROM freee_sync_queue WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            dh = conn.execute(
                "SELECT COUNT(*) AS c, COALESCE(SUM(quantity), 0) AS q "
                "FROM demand_history WHERE organization_id = ? AND source = 'csv'",
                (self.org_id,),
            ).fetchone()
        self.assertEqual(summary["imported"], 2)
        self.assertEqual(sales_after, sales_before)   # 売上台帳は不変
        self.assertEqual(moves_after, moves_before)   # 在庫元帳は不変
        self.assertEqual(queue_after, queue_before)   # freee 連携キューも不変
        self.assertEqual(dh["c"], 2)                  # 需要履歴に2件
        self.assertEqual(int(dh["q"]), 10)            # 数量合計 4+6

    def test_import_sales_history_idempotent_replace(self):
        # Phase D(⑤): 同じ CSV を2回取り込んでも demand_history は二重にならない（source='csv' 置換）。
        csv = (
            "date,sku,product_name,quantity,unit_price\n"
            "2026-03-01,SKU-DH2,需要商品2,4,1000\n"
            "2026-03-02,SKU-DH2,需要商品2,6,1000\n"
        )
        with app.get_conn() as conn:
            app.import_sales_history(conn, self.org_id, csv)
            app.import_sales_history(conn, self.org_id, csv)
            dh = conn.execute(
                "SELECT COUNT(*) AS c FROM demand_history WHERE organization_id = ? AND source = 'csv'",
                (self.org_id,),
            ).fetchone()["c"]
            products = conn.execute(
                "SELECT COUNT(*) AS c FROM products WHERE organization_id = ? AND sku = 'SKU-DH2'",
                (self.org_id,),
            ).fetchone()["c"]
        self.assertEqual(dh, 2)        # 二重取込でも2件のまま（置換）
        self.assertEqual(products, 1)  # 商品も重複作成しない（sku で照合）

    def test_clear_organization_data_clears_demand_history(self):
        # Phase D(⑤): クリーンスタートで需要履歴も消える。
        csv = "date,sku,product_name,quantity,unit_price\n2026-03-01,SKU-CLR,消す商品,3,500\n"
        with app.get_conn() as conn:
            app.import_sales_history(conn, self.org_id, csv)
            before = conn.execute(
                "SELECT COUNT(*) AS c FROM demand_history WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            self.assertGreater(before, 0)
            app.db.clear_organization_data(conn, self.org_id)
            after = conn.execute(
                "SELECT COUNT(*) AS c FROM demand_history WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
        self.assertEqual(after, 0)

    def test_clear_organization_data_empties_but_keeps_account(self):
        # A-9 クリーンスタート: 業務データは全消去・組織(アカウント)は残る。
        with app.get_conn() as conn:
            before = conn.execute(
                "SELECT COUNT(*) AS c FROM products WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            self.assertGreater(before, 0)  # seed 済み
            app.db.clear_organization_data(conn, self.org_id)
            for table in ("products", "sales", "purchases", "inventory_movements", "freee_sync_queue", "forecasts"):
                count = conn.execute(
                    f"SELECT COUNT(*) AS c FROM {table} WHERE organization_id = ?", (self.org_id,)
                ).fetchone()["c"]
                self.assertEqual(count, 0, f"{table} は空になるべき")
            org_count = conn.execute(
                "SELECT COUNT(*) AS c FROM organizations WHERE id = ?", (self.org_id,)
            ).fetchone()["c"]
            self.assertEqual(org_count, 1)  # アカウントは残る

    def test_clear_then_import_full_cycle_and_stale_ledger_contract(self):
        # A-9 実運用フロー: デモ商品で元帳を開く→クリーンスタート→CSV取込（新id採番）。
        # 取込自体は必ず成功する。一方、削除済みの「旧 product_id」で元帳を引くと 404 になる
        # ＝フロントは loadAll で旧 id を参照してはいけない（index_html.py の loadAll ガードの根拠）。
        csv = (
            "date,sku,product_name,quantity,unit_price\n"
            "2025-09-01,REAL-PEN,リアル ボールペン,5,120\n"
            "2025-09-02,REAL-PEN,リアル ボールペン,8,120\n"
            "2025-09-01,REAL-NOTE,リアル ノート,2,300\n"
            "2025-09-02,REAL-NOTE,リアル ノート,3,300\n"
        )
        with app.get_conn() as conn:
            old_id = self._first_product(conn)["id"]  # ユーザが元帳で開いていたデモ商品
            app.db.clear_organization_data(conn, self.org_id)
            summary = app.import_sales_history(conn, self.org_id, csv)
            self.assertGreater(summary["imported"], 0)
            self.assertEqual(summary["created_products"], 2)  # REAL-PEN / REAL-NOTE
            self.assertEqual(summary["skipped"], 0)
            new_products = conn.execute(
                "SELECT id FROM products WHERE organization_id = ? ORDER BY id", (self.org_id,)
            ).fetchall()
            # 旧 id は消えている＝この id で元帳を引くと "product not found"（フロントが避けるべき呼び出し）。
            with self.assertRaises(app.NotFoundError):
                app.product_ledger(conn, self.org_id, old_id)
            # 新 id では元帳・予測系が問題なく描画できる（取込後の画面が成立する）。
            for p in new_products:
                self.assertIn("ledger", app.product_ledger(conn, self.org_id, p["id"]))
                self.assertIn("actual", app.forecast_series(conn, self.org_id, p["id"]))

    def test_purchase_increases_stock_and_creates_freee_queue(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            before = app.stock_by_product(conn, self.org_id)[product["id"]]
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "テスト仕入先",
                "invoice_no": "INV-P-001",
                "transaction_date": "2026-06-01",
                "received_date": "2026-06-02",
                "quantity": 3,
                "unit_price": 1000,
                "tax_rate": 10,
                "tax_category": "課税仕入 10%",
                "due_date": "2026-06-30",
            })
            after = app.stock_by_product(conn, self.org_id)[product["id"]]
            queue = conn.execute("SELECT * FROM freee_sync_queue WHERE source_type = 'purchase' AND source_id = ?", (result["purchase_id"],)).fetchone()

        self.assertEqual(after, before + 3)
        self.assertIsNotNone(queue)
        self.assertEqual(queue["status"], "pending")
        self.assertIn("INV-P-001", queue["payload_json"])

    def test_sale_decreases_stock_and_creates_freee_queue(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            before = app.stock_by_product(conn, self.org_id)[product["id"]]
            result = app.create_sale(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "テスト得意先",
                "invoice_no": "ORD-S-001",
                "transaction_date": "2026-06-03",
                "quantity": 2,
                "unit_price": 1500,
                "tax_rate": 10,
                "tax_category": "課税売上 10%",
                "due_date": "2026-07-31",
            })
            after = app.stock_by_product(conn, self.org_id)[product["id"]]
            queue = conn.execute("SELECT * FROM freee_sync_queue WHERE source_type = 'sale' AND source_id = ?", (result["sale_id"],)).fetchone()

        self.assertEqual(after, before - 2)
        self.assertIsNotNone(queue)
        self.assertEqual(queue["status"], "pending")
        self.assertIn("ORD-S-001", queue["payload_json"])

    def test_sale_cannot_exceed_available_stock(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            current = app.stock_by_product(conn, self.org_id)[product["id"]]
            with self.assertRaises(ValueError):
                app.create_sale(conn, self.org_id, {
                    "product_id": product["id"],
                    "partner_name": "テスト得意先",
                    "invoice_no": "ORD-S-OVER",
                    "transaction_date": "2026-06-03",
                    "quantity": current + 1,
                    "unit_price": 1500,
                })

    def test_queue_is_unique_per_source_document(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "テスト仕入先",
                "invoice_no": "INV-P-002",
                "transaction_date": "2026-06-01",
                "received_date": "2026-06-02",
                "quantity": 1,
                "unit_price": 1000,
            })
            app.enqueue_freee_payload(conn, self.org_id, "purchase", result["purchase_id"])
            count = conn.execute(
                "SELECT COUNT(*) AS count FROM freee_sync_queue WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()["count"]

        self.assertEqual(count, 1)

    def test_send_queue_to_pseudo_freee_marks_queue_sent(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse({"ok": True, "pseudo_freee_deal_id": 101, "created": True, "duplicate": False})

        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "テスト仕入先",
                "invoice_no": "INV-P-SEND",
                "transaction_date": "2026-06-01",
                "received_date": "2026-06-02",
                "quantity": 1,
                "unit_price": 1000,
            })
            queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
            with patch("app.urllib.request.urlopen", fake_urlopen):
                send_result = app.send_queue_to_pseudo_freee(conn, self.org_id, {"id": queue["id"]})
            updated = conn.execute("SELECT * FROM freee_sync_queue WHERE id = ?", (queue["id"],)).fetchone()

        self.assertEqual(captured["url"], f"{app.PSEUDO_FREEE_API_URL}/api/deals")
        self.assertEqual(captured["timeout"], 8)
        self.assertEqual(captured["body"]["queue_id"], queue["id"])
        self.assertEqual(captured["body"]["source_type"], "purchase")
        self.assertIn("payload", captured["body"])
        self.assertEqual(send_result["external_accounting_id"], "pseudo-freee-101")
        self.assertEqual(updated["status"], "sent")
        self.assertEqual(updated["external_accounting_id"], "pseudo-freee-101")
        self.assertEqual(updated["sync_error_message"], "")

    def test_send_queue_to_pseudo_freee_marks_queue_failed_on_connection_error(self):
        def fake_urlopen(request, timeout):
            raise urllib.error.URLError("connection refused")

        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_sale(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "テスト得意先",
                "invoice_no": "ORD-S-SEND-FAIL",
                "transaction_date": "2026-06-03",
                "quantity": 1,
                "unit_price": 1500,
            })
            queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'sale' AND source_id = ?",
                (result["sale_id"],),
            ).fetchone()
            with patch("app.urllib.request.urlopen", fake_urlopen):
                with self.assertRaises(ValueError):
                    app.send_queue_to_pseudo_freee(conn, self.org_id, {"id": queue["id"]})
            updated = conn.execute("SELECT * FROM freee_sync_queue WHERE id = ?", (queue["id"],)).fetchone()

        self.assertEqual(updated["status"], "failed")
        self.assertIn("疑似freeeに接続できません", updated["sync_error_message"])

    def test_send_all_pending_queue_sends_all_pending(self):
        # Phase D①: 未送信(pending/failed/retry)を一括送信。全部 sent になり remaining=0。
        deal = {"n": 100}

        def fake_urlopen(request, timeout):
            deal["n"] += 1
            return FakeResponse({"ok": True, "pseudo_freee_deal_id": deal["n"], "duplicate": False})

        with app.get_conn() as conn:
            conn.execute("DELETE FROM freee_sync_queue WHERE organization_id = ?", (self.org_id,))
            product = self._first_product(conn)
            app.create_purchase(conn, self.org_id, {
                "product_id": product["id"], "partner_name": "仕入先A", "invoice_no": "P-ALL-1",
                "transaction_date": "2026-06-01", "received_date": "2026-06-01", "quantity": 2, "unit_price": 500,
            })
            app.create_purchase(conn, self.org_id, {
                "product_id": product["id"], "partner_name": "仕入先A", "invoice_no": "P-ALL-2",
                "transaction_date": "2026-06-02", "received_date": "2026-06-02", "quantity": 1, "unit_price": 800,
            })
            app.create_sale(conn, self.org_id, {
                "product_id": product["id"], "partner_name": "得意先B", "invoice_no": "S-ALL-1",
                "transaction_date": "2026-06-03", "quantity": 1, "unit_price": 1500,
            })
            self.assertEqual(app.count_unsent_queue(conn, self.org_id), 3)
            with patch("app.urllib.request.urlopen", fake_urlopen):
                result = app.send_all_pending_queue(conn, self.org_id)
            statuses = [
                r["status"] for r in conn.execute(
                    "SELECT status FROM freee_sync_queue WHERE organization_id = ?", (self.org_id,)
                ).fetchall()
            ]
        self.assertEqual(result["attempted"], 3)
        self.assertEqual(result["sent"], 3)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(result["remaining_unsent"], 0)
        self.assertTrue(all(s == "sent" for s in statuses))

    def test_send_all_queue_failure_then_retry_increments_count(self):
        # Phase D①: 疑似freee 停止中は failed＋retry_count++、復帰後に再度一括送信で sent。
        def failing_urlopen(request, timeout):
            raise urllib.error.URLError("connection refused")

        def ok_urlopen(request, timeout):
            return FakeResponse({"ok": True, "pseudo_freee_deal_id": 555, "duplicate": False})

        with app.get_conn() as conn:
            conn.execute("DELETE FROM freee_sync_queue WHERE organization_id = ?", (self.org_id,))
            product = self._first_product(conn)
            app.create_purchase(conn, self.org_id, {
                "product_id": product["id"], "partner_name": "仕入先C", "invoice_no": "P-RETRY",
                "transaction_date": "2026-06-01", "received_date": "2026-06-01", "quantity": 1, "unit_price": 1000,
            })
            with patch("app.urllib.request.urlopen", failing_urlopen):
                first = app.send_all_pending_queue(conn, self.org_id)
            after_fail = conn.execute(
                "SELECT status, retry_count FROM freee_sync_queue WHERE organization_id = ?", (self.org_id,)
            ).fetchone()
            with patch("app.urllib.request.urlopen", ok_urlopen):
                second = app.send_all_pending_queue(conn, self.org_id)
            after_ok = conn.execute(
                "SELECT status, retry_count FROM freee_sync_queue WHERE organization_id = ?", (self.org_id,)
            ).fetchone()
        self.assertEqual(first["sent"], 0)
        self.assertEqual(first["failed"], 1)
        self.assertEqual(after_fail["status"], "failed")
        self.assertEqual(after_fail["retry_count"], 1)
        self.assertEqual(second["sent"], 1)
        self.assertEqual(second["failed"], 0)
        self.assertEqual(second["remaining_unsent"], 0)
        self.assertEqual(after_ok["status"], "sent")
        self.assertEqual(after_ok["retry_count"], 1)  # 成功時はカウント据え置き

    def test_build_freee_payload_includes_partner_master_id(self):
        # Phase D⑥: payload に business_partners.id を載せる（共有ID連携の起点）。登録時の payload にも入る。
        with app.get_conn() as conn:
            product = self._first_product(conn)
            res = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"], "partner_name": "共有ID仕入先", "invoice_no": "P-PM-1",
                "transaction_date": "2026-06-01", "received_date": "2026-06-01", "quantity": 1, "unit_price": 1000,
            })
            bp = conn.execute(
                "SELECT id FROM business_partners WHERE organization_id = ? AND partner_type='supplier' AND partner_name='共有ID仕入先'",
                (self.org_id,),
            ).fetchone()
            payload = app.build_freee_payload(conn, self.org_id, "purchase", res["purchase_id"])
            queue = conn.execute(
                "SELECT payload_json FROM freee_sync_queue WHERE source_type='purchase' AND source_id=?",
                (res["purchase_id"],),
            ).fetchone()
        self.assertIsNotNone(bp)
        self.assertEqual(payload["partner_master_id"], bp["id"])
        # add_business_partner→enqueue の順なので、登録時に積まれた payload にも id が入っている。
        self.assertEqual(json.loads(queue["payload_json"])["partner_master_id"], bp["id"])

    def test_push_partner_rename_posts_to_pseudo_freee(self):
        # Phase D⑥: 改名を疑似freee へ push（/api/partner）。
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            return FakeResponse({"ok": True, "updated_deals": 3})

        with patch("app.urllib.request.urlopen", fake_urlopen):
            result = app.push_partner_rename(self.org_id, "supplier", 7, "旧名", "新名")
        self.assertEqual(captured["url"], f"{app.PSEUDO_FREEE_API_URL}/api/partner")
        self.assertEqual(captured["body"]["partner_master_id"], 7)
        self.assertEqual(captured["body"]["old_name"], "旧名")
        self.assertEqual(captured["body"]["new_name"], "新名")
        self.assertTrue(result["ok"])
        self.assertEqual(result["updated_deals"], 3)

    def test_push_partner_rename_graceful_on_connection_error(self):
        # 疑似freee が落ちていてもローカル改名は成立。push は ok:false を返すだけ。
        def fake_urlopen(request, timeout):
            raise urllib.error.URLError("refused")

        with patch("app.urllib.request.urlopen", fake_urlopen):
            result = app.push_partner_rename(self.org_id, "supplier", 7, "旧名", "新名")
        self.assertFalse(result["ok"])
        self.assertIn("接続できません", result["error"])

    def test_update_business_partner_returns_master_id(self):
        # Phase D⑥: 改名関数は id を返す（ルートが push に使う）。HTTP はしない＝DB のみ。
        with app.get_conn() as conn:
            product = self._first_product(conn)
            app.create_sale(conn, self.org_id, {
                "product_id": product["id"], "partner_name": "改名前得意先", "invoice_no": "S-RN-1",
                "transaction_date": "2026-06-03", "quantity": 1, "unit_price": 1500,
            })
            result = app.update_business_partner(conn, self.org_id, {
                "partner_type": "customer", "old_name": "改名前得意先", "new_name": "改名後得意先",
            })
            bp = conn.execute(
                "SELECT id FROM business_partners WHERE organization_id=? AND partner_type='customer' AND partner_name='改名後得意先'",
                (self.org_id,),
            ).fetchone()
        self.assertEqual(result["partner_master_id"], bp["id"])
        self.assertEqual(result["new_name"], "改名後得意先")

    def test_stock_by_product_as_of_filters_by_date(self):
        # Phase D④: as_of でその日までの在庫だけ合計する。
        with app.get_conn() as conn:
            app.create_product(conn, self.org_id, {"sku": "AS-OF-1", "product_name": "asof商品", "purchase_unit_price": 100})
            pid = conn.execute("SELECT id FROM products WHERE organization_id=? AND sku='AS-OF-1'", (self.org_id,)).fetchone()["id"]
            app.create_purchase(conn, self.org_id, {"product_id": pid, "partner_name": "as", "invoice_no": "AO-1", "transaction_date": "2026-06-10", "received_date": "2026-06-10", "quantity": 5, "unit_price": 100})
            app.create_purchase(conn, self.org_id, {"product_id": pid, "partner_name": "as", "invoice_no": "AO-2", "transaction_date": "2026-06-20", "received_date": "2026-06-20", "quantity": 3, "unit_price": 100})
            s_mid = app.stock_by_product(conn, self.org_id, as_of="2026-06-10").get(pid, 0)
            s_late = app.stock_by_product(conn, self.org_id, as_of="2026-06-20").get(pid, 0)
            s_now = app.stock_by_product(conn, self.org_id).get(pid, 0)
        self.assertEqual(s_mid, 5)
        self.assertEqual(s_late, 8)
        self.assertEqual(s_now, 8)

    def test_closing_inventory_book_amount_reflects_as_of(self):
        # Phase D④: 帳簿評価額 = Σ(在庫×仕入単価)。既存 seed の在庫移動を消して入庫1件だけで検証する。
        with app.get_conn() as conn:
            conn.execute("DELETE FROM inventory_movements WHERE organization_id = ?", (self.org_id,))
            app.create_product(conn, self.org_id, {"sku": "BK-1", "product_name": "簿価商品", "purchase_unit_price": 100})
            pid = conn.execute("SELECT id FROM products WHERE organization_id=? AND sku='BK-1'", (self.org_id,)).fetchone()["id"]
            app.create_purchase(conn, self.org_id, {"product_id": pid, "partner_name": "bk", "invoice_no": "BK-P-1", "transaction_date": "2026-06-10", "received_date": "2026-06-10", "quantity": 5, "unit_price": 100})
            before = app.closing_inventory_book_amount(conn, self.org_id, as_of="2026-06-09")  # 入庫前
            after = app.closing_inventory_book_amount(conn, self.org_id, as_of="2026-06-10")   # 入庫後
        self.assertEqual(before, 0.0)
        self.assertEqual(after, 500.0)  # 5 × 100

    def test_push_closing_inventory_posts_book_and_physical(self):
        # Phase D④: 帳簿評価額はサーバ側で再計算して送る。実地は上書き可（無ければ帳簿＝実地）。
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data.decode("utf-8"))
            body = captured["body"]
            return FakeResponse({"ok": True, "period": body["period"], "book_amount": body["book_amount"], "physical_amount": body["physical_amount"]})

        with app.get_conn() as conn:
            app.create_product(conn, self.org_id, {"sku": "PUSH-1", "product_name": "送信商品", "purchase_unit_price": 200})
            pid = conn.execute("SELECT id FROM products WHERE organization_id=? AND sku='PUSH-1'", (self.org_id,)).fetchone()["id"]
            app.create_purchase(conn, self.org_id, {"product_id": pid, "partner_name": "ps", "invoice_no": "PS-1", "transaction_date": "2026-06-10", "received_date": "2026-06-10", "quantity": 2, "unit_price": 200})
            expected_book = app.closing_inventory_book_amount(conn, self.org_id, as_of="2026-06-30")
            with patch("app.urllib.request.urlopen", fake_urlopen):
                r1 = app.push_closing_inventory(conn, self.org_id, {"period": "202606", "as_of": "2026-06-30"})
                r2 = app.push_closing_inventory(conn, self.org_id, {"period": "202606", "as_of": "2026-06-30", "physical_amount": 9999})
        self.assertEqual(captured["url"], f"{app.PSEUDO_FREEE_API_URL}/api/closing-inventory")
        self.assertEqual(captured["body"]["period"], "202606")
        self.assertEqual(r1["book_amount"], expected_book)
        self.assertEqual(r1["physical_amount"], expected_book)  # 上書きなし＝帳簿額
        self.assertEqual(r2["physical_amount"], 9999.0)         # 上書きあり

    def test_push_closing_inventory_rejects_bad_period(self):
        with app.get_conn() as conn:
            with self.assertRaises(ValueError):
                app.push_closing_inventory(conn, self.org_id, {"period": "2026-06"})

    def test_reconciliation_matches_and_detects_diff(self):
        # Phase D⑤: 在庫の税込総額（取消除外）と疑似freee の純額を比較。一致/差分/接続不可を判定。
        with app.get_conn() as conn:
            conn.execute("DELETE FROM inventory_movements WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM sales WHERE organization_id = ?", (self.org_id,))
            conn.execute("DELETE FROM purchases WHERE organization_id = ?", (self.org_id,))
            app.create_product(conn, self.org_id, {"sku": "RX", "product_name": "突合商品", "purchase_unit_price": 1000})
            pid = conn.execute("SELECT id FROM products WHERE organization_id=? AND sku='RX'", (self.org_id,)).fetchone()["id"]
            app.create_purchase(conn, self.org_id, {"product_id": pid, "partner_name": "r仕", "invoice_no": "RX-P", "transaction_date": "2026-06-10", "received_date": "2026-06-10", "quantity": 5, "unit_price": 1000, "tax_rate": 10})
            app.create_sale(conn, self.org_id, {"product_id": pid, "partner_name": "r得", "invoice_no": "RX-S", "transaction_date": "2026-06-11", "quantity": 2, "unit_price": 2000, "tax_rate": 10})
            # 在庫: 売上 round(2*2000*1.1)=4400 / 仕入 round(5*1000*1.1)=5500 / 期末在庫 3*1000=3000
            with patch("app._fetch_pseudo_freee_reconciliation", lambda: {"ok": True, "sales_total": 4400.0, "purchase_total": 5500.0, "merchandise": 3000.0}):
                r = app.reconciliation(conn, self.org_id)
            with patch("app._fetch_pseudo_freee_reconciliation", lambda: {"ok": True, "sales_total": 0.0, "purchase_total": 5500.0, "merchandise": 3000.0}):
                r_diff = app.reconciliation(conn, self.org_id)
            with patch("app._fetch_pseudo_freee_reconciliation", lambda: None):
                r_down = app.reconciliation(conn, self.org_id)
        sales = next(x for x in r["rows"] if x["label"] == "売上高")
        purch = next(x for x in r["rows"] if x["label"] == "仕入高")
        merch = next(x for x in r["rows"] if x["label"].startswith("期末在庫"))
        self.assertTrue(r["all_match"])
        self.assertEqual((sales["inventory"], sales["freee"]), (4400.0, 4400.0))
        self.assertEqual(purch["inventory"], 5500.0)
        self.assertEqual(merch["inventory"], 3000.0)
        # 売上が未送信で疑似freee に無い→差分検出
        self.assertFalse(r_diff["all_match"])
        s2 = next(x for x in r_diff["rows"] if x["label"] == "売上高")
        self.assertFalse(s2["match"])
        self.assertEqual(s2["diff"], 4400.0)
        # 疑似freee 接続不可
        self.assertFalse(r_down["freee_available"])
        self.assertIsNone(r_down["all_match"])

    def test_order_judgement_by_model_differs_per_model(self):
        # 需要予測レベル2: 選択商品の発注判定を3モデルで算出。MAE昇順・★最良・モデルで必要在庫/判定が変わる。
        from datetime import date as _date, timedelta as _td
        with app.get_conn() as conn:
            app.create_product(conn, self.org_id, {"sku": "JM-1", "product_name": "判定商品", "purchase_unit_price": 100, "safety_stock": 5, "lead_time_days": 3})
            pid = conn.execute("SELECT id FROM products WHERE organization_id=? AND sku='JM-1'", (self.org_id,)).fetchone()["id"]
            app.create_purchase(conn, self.org_id, {"product_id": pid, "partner_name": "s", "invoice_no": "JM-P", "transaction_date": "2026-06-01", "received_date": "2026-06-01", "quantity": 20, "unit_price": 100})
            app.create_sale(conn, self.org_id, {"product_id": pid, "partner_name": "c", "invoice_no": "JM-S", "transaction_date": "2026-06-02", "quantity": 2, "unit_price": 150})
            for mname, mae in [("baseline", 4.0), ("lightgbm", 4.1), ("sarima", 4.2)]:
                conn.execute("INSERT INTO model_evaluations (organization_id, model_name, period, mae, mape) VALUES (?,?,?,?,?)", (self.org_id, mname, "test", mae, 50.0))
            preds = {"baseline": [5, 5, 5], "lightgbm": [1, 1, 1], "sarima": [4, 4, 4]}
            base = _date(2026, 7, 1)
            for mname, vals in preds.items():
                for i, v in enumerate(vals):
                    conn.execute(
                        "INSERT INTO forecasts (organization_id, product_id, target_date, model_name, predicted_quantity, lower, upper) VALUES (?,?,?,?,?,?,?)",
                        (self.org_id, pid, (base + _td(days=i)).isoformat(), mname, v, v, v),
                    )
            result = app.order_judgement_by_model(conn, self.org_id, pid)
        by = {m["model_name"]: m for m in result["models"]}
        self.assertEqual([m["model_name"] for m in result["models"]], ["baseline", "lightgbm", "sarima"])  # MAE 昇順
        self.assertTrue(by["baseline"]["is_best"])
        self.assertEqual(result["product"]["product_name"], "判定商品")
        self.assertEqual(result["stock_quantity"], 18)  # 仕入20 − 売上2
        # baseline: ltd=15, 必要在庫=20, 発注量=max(20-18,0)=2 → 発注推奨
        self.assertEqual(by["baseline"]["required_inventory"], 20)
        self.assertEqual(by["baseline"]["recommended_order_quantity"], 2)
        self.assertEqual(by["baseline"]["judgement"], "発注推奨")
        # lightgbm: ltd=3, 必要在庫=8, 発注量=0 → 発注不要
        self.assertEqual(by["lightgbm"]["required_inventory"], 8)
        self.assertEqual(by["lightgbm"]["recommended_order_quantity"], 0)
        self.assertEqual(by["lightgbm"]["judgement"], "発注不要")
        # sarima: ltd=12, 必要在庫=17
        self.assertEqual(by["sarima"]["required_inventory"], 17)

    def test_send_queue_to_pseudo_freee_rejects_sent_queue(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "テスト仕入先",
                "invoice_no": "INV-P-RESENT",
                "transaction_date": "2026-06-01",
                "received_date": "2026-06-02",
                "quantity": 1,
                "unit_price": 1000,
            })
            queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
            app.mark_queue_status(conn, self.org_id, {"id": queue["id"], "status": "sent", "external_accounting_id": "pseudo-freee-1"})
            with self.assertRaises(ValueError):
                app.send_queue_to_pseudo_freee(conn, self.org_id, {"id": queue["id"]})

    def test_business_partners_are_seeded_from_existing_data(self):
        with app.get_conn() as conn:
            partners = app.list_business_partners(conn, self.org_id)

        self.assertIn("東京サプライ", partners["suppliers"])
        self.assertIn("青山ECストア", partners["customers"])

    def test_create_business_partner_adds_selectable_partner(self):
        with app.get_conn() as conn:
            app.create_business_partner(conn, self.org_id, {
                "partner_type": "customer",
                "partner_name": "テスト販売先",
            })
            partners = app.list_business_partners(conn, self.org_id)

        self.assertIn("テスト販売先", partners["customers"])

    # --- 取引先マスタの編集・削除（誤登録の訂正）---

    def test_update_business_partner_renames_master_and_past_transactions(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            app.create_purchase(conn, self.org_id, {
                "product_id": product["id"], "partner_name": "テスト仕入先A",
                "invoice_no": "INV-RENAME", "transaction_date": "2026-06-01",
                "received_date": "2026-06-02", "quantity": 1, "unit_price": 1000,
            })
            app.update_business_partner(conn, self.org_id, {
                "partner_type": "supplier", "old_name": "テスト仕入先A", "new_name": "テスト仕入先B",
            })
            after = app.list_business_partners(conn, self.org_id)
            purchase_partner = conn.execute(
                "SELECT partner_name FROM purchases WHERE organization_id = ? AND invoice_no = 'INV-RENAME'",
                (self.org_id,),
            ).fetchone()["partner_name"]

        self.assertNotIn("テスト仕入先A", after["suppliers"])
        self.assertIn("テスト仕入先B", after["suppliers"])
        # マスタだけでなく過去取引（purchases）の表示名も揃う。
        self.assertEqual(purchase_partner, "テスト仕入先B")

    def test_update_business_partner_rejects_duplicate_name(self):
        with app.get_conn() as conn:
            app.create_business_partner(conn, self.org_id, {"partner_type": "customer", "partner_name": "得意先X"})
            app.create_business_partner(conn, self.org_id, {"partner_type": "customer", "partner_name": "得意先Y"})
            with self.assertRaises(ValueError):
                app.update_business_partner(conn, self.org_id, {
                    "partner_type": "customer", "old_name": "得意先X", "new_name": "得意先Y",
                })

    def test_update_business_partner_missing_raises_not_found(self):
        with app.get_conn() as conn:
            with self.assertRaises(app.NotFoundError):
                app.update_business_partner(conn, self.org_id, {
                    "partner_type": "supplier", "old_name": "存在しない取引先", "new_name": "新名",
                })

    def test_delete_business_partner_removes_unused(self):
        with app.get_conn() as conn:
            app.create_business_partner(conn, self.org_id, {"partner_type": "supplier", "partner_name": "未使用仕入先"})
            app.delete_business_partner(conn, self.org_id, {"partner_type": "supplier", "partner_name": "未使用仕入先"})
            after = app.list_business_partners(conn, self.org_id)

        self.assertNotIn("未使用仕入先", after["suppliers"])

    def test_delete_business_partner_blocked_when_referenced(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            app.create_purchase(conn, self.org_id, {
                "product_id": product["id"], "partner_name": "参照あり仕入先",
                "invoice_no": "INV-REF", "transaction_date": "2026-06-01",
                "received_date": "2026-06-02", "quantity": 1, "unit_price": 1000,
            })
            # 取引のある取引先は削除できない（直すなら編集）。
            with self.assertRaises(ValueError):
                app.delete_business_partner(conn, self.org_id, {"partner_type": "supplier", "partner_name": "参照あり仕入先"})

    def test_cancel_purchase_adds_reversal_movement(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "テスト仕入先",
                "invoice_no": "INV-P-CANCEL",
                "transaction_date": "2026-06-01",
                "received_date": "2026-06-02",
                "quantity": 2,
                "unit_price": 1000,
            })
            movement = conn.execute(
                "SELECT * FROM inventory_movements WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
            before_cancel = app.stock_by_product(conn, self.org_id)[product["id"]]
            app.cancel_inventory_movement(conn, self.org_id, {"movement_id": movement["id"], "reason": "商品選択ミス"})
            after_cancel = app.stock_by_product(conn, self.org_id)[product["id"]]
            queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()

        self.assertEqual(after_cancel, before_cancel - 2)
        # 取消した仕訳は 'cancelled' になり、freee 送信待ちから外れて再送もできない。
        self.assertEqual(queue["status"], "cancelled")
        self.assertIn("商品選択ミス", queue["sync_error_message"])

    def test_cancelled_movement_leaves_freee_queue_and_cannot_be_sent(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "テスト仕入先",
                "invoice_no": "INV-P-CANCEL-QUEUE",
                "transaction_date": "2026-06-01",
                "received_date": "2026-06-02",
                "quantity": 2,
                "unit_price": 1000,
            })
            movement = conn.execute(
                "SELECT * FROM inventory_movements WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
            queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
            # 取消前は送信待ちキューに出る。
            queue_ids_before = {q["id"] for q in app.list_queue(conn, self.org_id)}
            self.assertIn(queue["id"], queue_ids_before)

            app.cancel_inventory_movement(conn, self.org_id, {"movement_id": movement["id"], "reason": "入力ミスのため取消"})

            # 取消後は送信待ちキューから消える。
            queue_ids_after = {q["id"] for q in app.list_queue(conn, self.org_id)}
            self.assertNotIn(queue["id"], queue_ids_after)
            # 直接送信しようとしても拒否される。
            with self.assertRaises(ValueError):
                app.send_queue_to_pseudo_freee(conn, self.org_id, {"id": queue["id"]})

    def test_cancel_movement_cannot_be_cancelled_twice(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_sale(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "テスト得意先",
                "invoice_no": "ORD-S-CANCEL",
                "transaction_date": "2026-06-03",
                "quantity": 1,
                "unit_price": 1500,
            })
            movement = conn.execute(
                "SELECT * FROM inventory_movements WHERE source_type = 'sale' AND source_id = ?",
                (result["sale_id"],),
            ).fetchone()
            app.cancel_inventory_movement(conn, self.org_id, {"movement_id": movement["id"], "reason": "数量ミス"})
            with self.assertRaises(ValueError):
                app.cancel_inventory_movement(conn, self.org_id, {"movement_id": movement["id"], "reason": "再取消"})

    # --- 仕入の入庫基準（日付の一本化）と取消日の整合 ---

    def test_freee_payload_uses_received_date_as_issue_date(self):
        # 仕入は入庫基準: freee の issue_date は入庫日(received_date)。在庫元帳の日付とも一致する。
        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"], "partner_name": "テスト仕入先",
                "invoice_no": "INV-DATE", "transaction_date": "2026-05-20",
                "received_date": "2026-05-22", "quantity": 1, "unit_price": 1000,
            })
            payload = app.build_freee_payload(conn, self.org_id, "purchase", result["purchase_id"])
            movement_date = conn.execute(
                "SELECT movement_date FROM inventory_movements WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()["movement_date"]
        self.assertEqual(payload["issue_date"], "2026-05-22")   # freee も入庫日
        self.assertEqual(movement_date, "2026-05-22")            # 在庫元帳も入庫日＝一致

    def test_cancel_uses_original_date_not_today(self):
        # 取消行は元取引の日付で相殺する（today ではない）＝元と同じ月で相殺され月次が合う。
        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"], "partner_name": "テスト仕入先",
                "invoice_no": "INV-CANCEL-DATE", "transaction_date": "2026-05-20",
                "received_date": "2026-05-22", "quantity": 1, "unit_price": 1000,
            })
            original = conn.execute(
                "SELECT * FROM inventory_movements WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
            app.cancel_inventory_movement(conn, self.org_id, {"movement_id": original["id"], "reason": "誤発注"})
            correction_date = conn.execute(
                "SELECT movement_date FROM inventory_movements WHERE movement_type = 'purchase_cancel' AND source_id = ?",
                (original["id"],),
            ).fetchone()["movement_date"]
        self.assertEqual(correction_date, original["movement_date"])  # 元と同じ日付
        self.assertEqual(correction_date, "2026-05-22")

    # --- Phase C: 送信済みの取消を疑似freee へ伝播（reverse-and-repost）---

    def _create_and_send_purchase(self, conn, invoice_no, deal_id=201):
        """仕入を作り、疑似freee 送信済みにして (purchase_id, queue) を返す（HTTP はモック）。"""
        result = app.create_purchase(conn, self.org_id, {
            "product_id": self._first_product(conn)["id"],
            "partner_name": "テスト仕入先",
            "invoice_no": invoice_no,
            "transaction_date": "2026-06-01",
            "received_date": "2026-06-02",
            "quantity": 3,
            "unit_price": 1000,
            "due_date": "2026-07-31",
        })

        def fake_urlopen(request, timeout):
            return FakeResponse({"ok": True, "pseudo_freee_deal_id": deal_id, "duplicate": False})

        queue = conn.execute(
            "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase' AND source_id = ?",
            (result["purchase_id"],),
        ).fetchone()
        with patch("app.urllib.request.urlopen", fake_urlopen):
            app.send_queue_to_pseudo_freee(conn, self.org_id, {"id": queue["id"]})
        return result["purchase_id"], queue

    def test_cancel_payload_is_sign_reversed(self):
        # 取消仕訳の payload は元仕訳の数量・金額をマイナスにし、memo に「取消」が入る。
        with app.get_conn() as conn:
            purchase_id, _ = self._create_and_send_purchase(conn, "INV-P-NEG")
            base = app.build_freee_payload(conn, self.org_id, "purchase", purchase_id)
            cancel = app.build_freee_payload(conn, self.org_id, "purchase_cancel", purchase_id)

        self.assertGreater(base["details"][0]["amount"], 0)
        self.assertEqual(cancel["details"][0]["amount"], -base["details"][0]["amount"])
        self.assertEqual(cancel["details"][0]["quantity"], -base["details"][0]["quantity"])
        self.assertIn("取消", cancel["memo"])
        # 元の due_date / type は保持される（疑似freee 側で同じ相手科目に展開され相殺できる）。
        self.assertEqual(cancel["due_date"], base["due_date"])
        self.assertEqual(cancel["type"], base["type"])

    def test_cancel_sent_purchase_enqueues_reversal(self):
        # 送信済み(sent)の仕入を取り消すと、purchase_cancel の取消仕訳がキューに積まれる。
        with app.get_conn() as conn:
            purchase_id, sent_queue = self._create_and_send_purchase(conn, "INV-P-SENT-CANCEL")
            movement = conn.execute(
                "SELECT * FROM inventory_movements WHERE source_type = 'purchase' AND source_id = ?",
                (purchase_id,),
            ).fetchone()
            result = app.cancel_inventory_movement(conn, self.org_id, {"movement_id": movement["id"], "reason": "誤発注"})

            cancel_queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase_cancel' AND source_id = ?",
                (purchase_id,),
            ).fetchone()
            sent_after = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE id = ?", (sent_queue["id"],)
            ).fetchone()

        self.assertTrue(result["cancel_queued"])
        # 取消仕訳が pending で積まれる（送信ボタンで反映する運用）。
        self.assertIsNotNone(cancel_queue)
        self.assertEqual(cancel_queue["status"], "pending")
        self.assertEqual(cancel_queue["direction"], "expense")
        self.assertLess(json.loads(cancel_queue["payload_json"])["details"][0]["amount"], 0)
        # 元の送信済み行はそのまま残る（監査証跡）。
        self.assertEqual(sent_after["status"], "sent")

    def test_cancel_unsent_purchase_does_not_enqueue_reversal(self):
        # 未送信(pending)の仕入を取り消すと、従来どおり cancelled にするだけ（取消仕訳は積まない）。
        with app.get_conn() as conn:
            result = app.create_purchase(conn, self.org_id, {
                "product_id": self._first_product(conn)["id"],
                "partner_name": "テスト仕入先",
                "invoice_no": "INV-P-UNSENT-CANCEL",
                "transaction_date": "2026-06-01",
                "received_date": "2026-06-02",
                "quantity": 2,
                "unit_price": 1000,
            })
            movement = conn.execute(
                "SELECT * FROM inventory_movements WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
            cancel_result = app.cancel_inventory_movement(conn, self.org_id, {"movement_id": movement["id"], "reason": "誤入力"})
            cancel_queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase_cancel' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()

        self.assertFalse(cancel_result["cancel_queued"])
        self.assertIsNone(cancel_queue)

    def test_send_cancel_queue_maps_to_base_source_type(self):
        # 取消仕訳(purchase_cancel)を送信するとき、疑似freee へは base 型(purchase)で POST する。
        with app.get_conn() as conn:
            purchase_id, _ = self._create_and_send_purchase(conn, "INV-P-CANCEL-SEND")
            movement = conn.execute(
                "SELECT * FROM inventory_movements WHERE source_type = 'purchase' AND source_id = ?",
                (purchase_id,),
            ).fetchone()
            app.cancel_inventory_movement(conn, self.org_id, {"movement_id": movement["id"], "reason": "誤発注"})
            cancel_queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase_cancel' AND source_id = ?",
                (purchase_id,),
            ).fetchone()

            captured = {}

            def fake_urlopen(request, timeout):
                captured["body"] = json.loads(request.data.decode("utf-8"))
                return FakeResponse({"ok": True, "pseudo_freee_deal_id": 999, "duplicate": False})

            with patch("app.urllib.request.urlopen", fake_urlopen):
                app.send_queue_to_pseudo_freee(conn, self.org_id, {"id": cancel_queue["id"]})
            updated = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE id = ?", (cancel_queue["id"],)
            ).fetchone()

        # 疑似freee の source_type CHECK は purchase/sale/manual のみ → base 型へ戻して送る。
        self.assertEqual(captured["body"]["source_type"], "purchase")
        self.assertEqual(captured["body"]["queue_id"], cancel_queue["id"])
        # payload は符号反転済み（マイナス）で渡る。
        self.assertLess(captured["body"]["payload"]["details"][0]["amount"], 0)
        self.assertEqual(updated["status"], "sent")

    def test_recommended_order_is_exact_shortage_from_required_level(self):
        product = {
            "stock_quantity": 38,
            "reorder_point": 30,
            "safety_stock": 20,
            "min_order_quantity": 10,
        }

        self.assertEqual(app.recommended_order_quantity(product, 38), 12)
        self.assertEqual(app.stock_status(product), "必要水準割れ")

    def test_stock_at_required_level_is_normal(self):
        product = {
            "stock_quantity": 28,
            "reorder_point": 18,
            "safety_stock": 10,
        }

        self.assertEqual(app.recommended_order_quantity(product, 28), 0)
        self.assertEqual(app.stock_status(product), "正常")

    def test_demo_history_is_seeded_for_forecasting(self):
        with app.get_conn() as conn:
            demo_sales = conn.execute(
                "SELECT COUNT(*) AS count FROM sales WHERE organization_id = ? AND invoice_no LIKE 'DEMO-HIST-S-%'",
                (self.org_id,),
            ).fetchone()["count"]
            demo_purchases = conn.execute(
                "SELECT COUNT(*) AS count FROM purchases WHERE organization_id = ? AND invoice_no LIKE 'DEMO-HIST-P-%'",
                (self.org_id,),
            ).fetchone()["count"]

        self.assertGreater(demo_sales, 0)
        self.assertGreater(demo_purchases, 0)

    def test_initial_stock_precedes_demo_history(self):
        with app.get_conn() as conn:
            row = conn.execute(
                """
                SELECT
                    MAX(CASE WHEN im.movement_type = 'initial_stock' THEN im.movement_date END) AS initial_date,
                    MIN(CASE WHEN im.note LIKE 'デモ%' THEN im.movement_date END) AS first_demo_date
                FROM inventory_movements im
                WHERE im.organization_id = ?
                """,
                (self.org_id,),
            ).fetchone()

        self.assertLess(row["initial_date"], row["first_demo_date"])

    def test_forecast_simulation_returns_product_rows(self):
        with app.get_conn() as conn:
            result = app.forecast_simulation(conn, self.org_id, 30)

        self.assertEqual(result["horizon_days"], 30)
        self.assertEqual(len(result["rows"]), 3)
        first = result["rows"][0]
        self.assertIn("month_end_forecast", first)
        self.assertIn("required_inventory", first)
        self.assertIn("recommended_order_quantity", first)
        self.assertIn("month_end_shortage", first)
        self.assertIn("lead_time_judgement", first)
        self.assertIn("month_end_judgement", first)

    def test_dashboard_inventory_uses_forecast_required_inventory(self):
        with app.get_conn() as conn:
            dashboard = app.dashboard(conn, self.org_id)
            forecast = app.forecast_simulation(conn, self.org_id, 30)

        forecast_by_sku = {row["sku"]: row for row in forecast["rows"]}
        for product in dashboard["products"]:
            forecast_row = forecast_by_sku[product["sku"]]
            self.assertEqual(product["required_stock_level"], forecast_row["required_inventory"])
            self.assertEqual(product["recommended_order_quantity"], forecast_row["recommended_order_quantity"])
            if forecast_row["recommended_order_quantity"] == 0 and product["stock_quantity"] > 0:
                self.assertEqual(product["status"], "正常")

    def test_forecast_handles_product_without_sales_history(self):
        with app.get_conn() as conn:
            app.create_product(conn, self.org_id, {
                "sku": "SKU-NODATA-001",
                "product_name": "履歴なし商品",
                "category": "テスト",
                "supplier_name": "テスト仕入先",
                "purchase_unit_price": 100,
                "sales_unit_price": 200,
                "safety_stock": 5,
                "reorder_point": 10,
            })
            result = app.forecast_simulation(conn, self.org_id, 30)

        no_data = next(row for row in result["rows"] if row["sku"] == "SKU-NODATA-001")
        self.assertEqual(no_data["judgement"], "データ不足")
        self.assertEqual(no_data["recent_sales_quantity"], 0)

    def test_product_ledger_is_returned_newest_first(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            ledger = app.product_ledger(conn, self.org_id, product["id"])["ledger"]

        dates = [row["movement_date"] for row in ledger]
        self.assertEqual(dates, sorted(dates, reverse=True))


if __name__ == "__main__":
    unittest.main()
