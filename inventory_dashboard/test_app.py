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
