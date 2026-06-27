from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

import app


def sample_deal(queue_id: int = 12) -> dict:
    return {
        "queue_id": queue_id,
        "source_type": "purchase",
        "source_id": 4,
        "payload": {
            "api_target": "freee_accounting_deal",
            "issue_date": "2026-06-12",
            "due_date": "2026-07-31",
            "type": "expense",
            "partner_master_id": 1,
            "partner_name": "東京サプライ",
            "freee_partner_id": "",
            "invoice_no": "P-202606-002",
            "details": [
                {
                    "sku": "SKU-USB-C-001",
                    "description": "USB-Cケーブル 1m",
                    "quantity": 25,
                    "unit_price": 490,
                    "tax_rate": 10,
                    "tax_category": "課税仕入 10%",
                    "amount": 13475,
                    "account_item_name": "仕入高",
                }
            ],
        },
    }


class PseudoFreeeAppTest(unittest.TestCase):
    def setUp(self) -> None:
        # A-8: .env に DATABASE_URL があっても、このテストは必ずローカル SQLite を使う。
        self._saved_db_url = os.environ.pop("DATABASE_URL", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = app.DB_PATH
        app.DB_PATH = Path(self.tmp.name) / "pseudo_freee_test.db"
        app.init_db()

    def tearDown(self) -> None:
        app.DB_PATH = self.original_db_path
        self.tmp.cleanup()
        if self._saved_db_url is not None:
            os.environ["DATABASE_URL"] = self._saved_db_url

    def test_create_deal_saves_summary_lines_and_payload_json(self) -> None:
        with app.db_connection() as conn:
            deal_id, created = app.create_deal(conn, sample_deal())
            deal = app.get_deal(conn, deal_id)

        self.assertTrue(created)
        self.assertIsNotNone(deal)
        assert deal is not None
        self.assertEqual(deal["queue_id"], 12)
        self.assertEqual(deal["source_type"], "purchase")
        self.assertEqual(deal["deal_type"], "expense")
        self.assertEqual(deal["partner_name"], "東京サプライ")
        self.assertEqual(deal["amount"], 13475)
        self.assertIn('"api_target": "freee_accounting_deal"', deal["payload_json"])
        self.assertEqual(len(deal["lines"]), 1)
        self.assertEqual(deal["lines"][0]["account_item_name"], "仕入高")

    def test_create_deal_returns_existing_id_for_duplicate_source_queue(self) -> None:
        with app.db_connection() as conn:
            first_id, first_created = app.create_deal(conn, sample_deal())
            second_id, second_created = app.create_deal(conn, sample_deal())
            deals = app.list_deals(conn)

        self.assertTrue(first_created)
        self.assertFalse(second_created)
        self.assertEqual(second_id, first_id)
        self.assertEqual(len(deals), 1)

    def _deal(self, queue_id: int, source_id: int, master_id, partner_name: str, source_type: str = "purchase") -> dict:
        d = sample_deal(queue_id)
        d["source_type"] = source_type
        d["source_id"] = source_id
        d["payload"]["type"] = "expense" if source_type == "purchase" else "income"
        d["payload"]["partner_master_id"] = master_id
        d["payload"]["partner_name"] = partner_name
        return d

    def test_create_deal_registers_payee(self) -> None:
        # Phase D⑥: 在庫から来た取引先も payee マスタに登録される（手入力経費と同様に名寄せ候補に出る）。
        with app.db_connection() as conn:
            app.create_deal(conn, sample_deal())
            row = conn.execute(
                "SELECT 1 FROM pseudo_freee_payees WHERE payee_name = ?", ("東京サプライ",)
            ).fetchone()
        self.assertIsNotNone(row)

    def test_rename_partner_updates_deals_by_master_id(self) -> None:
        # Phase D⑥: partner_master_id でひもづく送信済み deal の取引先名を一括で直す（別IDは不変）。
        with app.db_connection() as conn:
            app.create_deal(conn, self._deal(queue_id=101, source_id=11, master_id=1, partner_name="東京サプライ"))
            app.create_deal(conn, self._deal(queue_id=102, source_id=12, master_id=2, partner_name="大阪商店"))
            result = app.rename_partner(
                conn, {"partner_master_id": 1, "old_name": "東京サプライ", "new_name": "東京サプライ商事"}
            )
            d1 = conn.execute("SELECT partner_name FROM pseudo_freee_deals WHERE partner_master_id = 1").fetchone()
            d2 = conn.execute("SELECT partner_name FROM pseudo_freee_deals WHERE partner_master_id = 2").fetchone()
            payee_new = conn.execute("SELECT 1 FROM pseudo_freee_payees WHERE payee_name = '東京サプライ商事'").fetchone()
            payee_old = conn.execute("SELECT 1 FROM pseudo_freee_payees WHERE payee_name = '東京サプライ'").fetchone()
        self.assertEqual(result["updated_deals"], 1)
        self.assertEqual(d1["partner_name"], "東京サプライ商事")
        self.assertEqual(d2["partner_name"], "大阪商店")
        self.assertIsNotNone(payee_new)
        self.assertIsNone(payee_old)

    def test_rename_partner_name_fallback_for_legacy_deal(self) -> None:
        # Phase D⑥: partner_master_id 無し（D-3前に送信した）deal も、在庫由来なら名前一致で直せる。
        with app.db_connection() as conn:
            deal_id, _ = app.create_deal(
                conn, self._deal(queue_id=103, source_id=13, master_id=1, partner_name="名前のみ商店")
            )
            conn.execute("UPDATE pseudo_freee_deals SET partner_master_id = NULL WHERE id = ?", (deal_id,))
            result = app.rename_partner(
                conn, {"partner_master_id": None, "old_name": "名前のみ商店", "new_name": "名前のみ商店NEW"}
            )
            d = conn.execute("SELECT partner_name FROM pseudo_freee_deals WHERE id = ?", (deal_id,)).fetchone()
        self.assertEqual(result["updated_deals"], 1)
        self.assertEqual(d["partner_name"], "名前のみ商店NEW")

    def test_upsert_closing_inventory_insert_then_update(self) -> None:
        # Phase B/D-4: 在庫からの期末棚卸を受信→保存。同 period の再送は upsert。BS 商品 = physical_amount。
        with app.db_connection() as conn:
            app.upsert_closing_inventory(conn, {"period": "202606", "book_amount": 400, "physical_amount": 380})
            cur = app.closing_inventory_current(conn)
            phys1 = app.closing_inventory_physical_amount(conn)
            app.upsert_closing_inventory(conn, {"period": "202606", "book_amount": 500, "physical_amount": 500})
            phys2 = app.closing_inventory_physical_amount(conn)
        self.assertEqual(cur["period"], "202606")
        self.assertEqual(phys1, 380.0)
        self.assertEqual(phys2, 500.0)

    def test_upsert_closing_inventory_defaults_physical_to_book(self) -> None:
        # 実地未指定なら帳簿＝実地。
        with app.db_connection() as conn:
            result = app.upsert_closing_inventory(conn, {"period": "202607", "book_amount": 1234})
        self.assertEqual(result["physical_amount"], 1234.0)

    def test_reconciliation_totals_sums_income_purchase_and_merchandise(self) -> None:
        # Phase D⑤: 突合用の素の合計。売上高=income deal、仕入高=source_type purchase、商品=期末実地。
        with app.db_connection() as conn:
            app.create_deal(conn, self._deal(queue_id=201, source_id=21, master_id=1, partner_name="売A", source_type="sale"))
            app.create_deal(conn, self._deal(queue_id=202, source_id=22, master_id=1, partner_name="仕A", source_type="purchase"))
            app.upsert_closing_inventory(conn, {"period": "202609", "book_amount": 50000, "physical_amount": 50000})
            totals = app.reconciliation_totals(conn)
        # sample_deal の明細金額は 13475（売上=income / 仕入=purchase でそれぞれ計上）
        self.assertEqual(totals["sales_total"], 13475.0)
        self.assertEqual(totals["purchase_total"], 13475.0)
        self.assertEqual(totals["merchandise"], 50000.0)

    def test_create_manual_expense_saves_snapshot_and_updates_summary(self) -> None:
        with app.db_connection() as conn:
            result = app.create_manual_expense(
                conn,
                {
                    "issue_date": "2026-06-13",
                    "due_date": "2026-06-30",
                    "partner_name": "日本橋文具",
                    "account_item_name": "消耗品費",
                    "tax_category": "課税仕入 10%",
                    "amount": 3300,
                    "description": "梱包資材",
                    "memo": "手入力テスト",
                },
            )
            deal = app.get_deal(conn, result["pseudo_freee_deal_id"])
            deals = app.list_deals(conn, {"source_type": "manual_expense"})

        self.assertIsNotNone(deal)
        assert deal is not None
        self.assertEqual(deal["source_app"], "manual")
        self.assertEqual(deal["source_type"], "manual_expense")
        self.assertIsNone(deal["queue_id"])
        self.assertEqual(deal["partner_name"], "日本橋文具")
        self.assertEqual(deal["memo"], "手入力テスト")
        self.assertIn("pseudo_freee_manual_expense", deal["payload_json"])
        self.assertEqual(len(deals), 1)

    def test_payment_method_controls_due_date(self) -> None:
        base = {"issue_date": "2026-06-13", "partner_name": "日本橋文具", "account_item_name": "消耗品費"}
        with app.db_connection() as conn:
            r1 = app.create_manual_expense(conn, {**base, "amount": 3300, "payment_method": "未払金", "due_date": "2026-06-30"})
            d1 = app.get_deal(conn, r1["pseudo_freee_deal_id"])
            r2 = app.create_manual_expense(conn, {**base, "amount": 1200, "payment_method": "現金", "due_date": "2026-06-30"})
            d2 = app.get_deal(conn, r2["pseudo_freee_deal_id"])
            r3 = app.create_manual_expense(conn, {**base, "amount": 500})  # 既定=現金
            d3 = app.get_deal(conn, r3["pseudo_freee_deal_id"])

        # 未払金: 支払予定日を保持。現金: クリア。既定は現金。
        self.assertEqual(d1["payment_method"], "未払金")
        self.assertEqual(d1["due_date"], "2026-06-30")
        self.assertEqual(d2["payment_method"], "現金")
        self.assertEqual(d2["due_date"], "")
        self.assertEqual(d3["payment_method"], "現金")
        self.assertEqual(d3["due_date"], "")

    def test_invalid_payment_method_raises(self) -> None:
        with app.db_connection() as conn:
            with self.assertRaises(ValueError):
                app.create_manual_expense(
                    conn,
                    {"issue_date": "2026-06-13", "partner_name": "X", "account_item_name": "消耗品費", "amount": 100, "payment_method": "クレカ"},
                )

    def test_update_manual_expense_changes_fields(self) -> None:
        with app.db_connection() as conn:
            created = app.create_manual_expense(
                conn,
                {"issue_date": "2026-06-13", "partner_name": "日本橋文具", "account_item_name": "消耗品費", "amount": 3300, "payment_method": "現金"},
            )
            deal_id = created["pseudo_freee_deal_id"]
            app.update_manual_expense(
                conn,
                deal_id,
                {
                    "issue_date": "2026-06-20", "partner_name": "東京サプライ", "account_item_name": "会議費",
                    "tax_category": "課税仕入 8%", "amount": 5000, "payment_method": "未払金",
                    "due_date": "2026-07-31", "memo": "更新後メモ",
                },
            )
            deal = app.get_deal(conn, deal_id)

        assert deal is not None
        self.assertEqual(deal["partner_name"], "東京サプライ")
        self.assertEqual(deal["account_item_name"], "会議費")
        self.assertEqual(deal["amount"], 5000)
        self.assertEqual(deal["payment_method"], "未払金")
        self.assertEqual(deal["due_date"], "2026-07-31")
        self.assertEqual(deal["memo"], "更新後メモ")
        self.assertEqual(len(deal["lines"]), 1)
        self.assertEqual(deal["lines"][0]["amount"], 5000)

    def test_delete_deal_removes_deal_and_lines(self) -> None:
        with app.db_connection() as conn:
            created = app.create_manual_expense(
                conn,
                {"issue_date": "2026-06-13", "partner_name": "日本橋文具", "account_item_name": "消耗品費", "amount": 3300},
            )
            deal_id = created["pseudo_freee_deal_id"]
            self.assertTrue(app.delete_deal(conn, deal_id))
            self.assertIsNone(app.get_deal(conn, deal_id))
            lines = conn.execute(
                "SELECT COUNT(*) AS c FROM pseudo_freee_deal_lines WHERE deal_id = ?", (deal_id,)
            ).fetchone()["c"]
            self.assertEqual(lines, 0)
            self.assertFalse(app.delete_deal(conn, 9999))

    def test_cannot_edit_synced_deal(self) -> None:
        with app.db_connection() as conn:
            deal_id, _ = app.create_deal(conn, sample_deal())
            with self.assertRaises(ValueError):
                app.update_manual_expense(
                    conn, deal_id, {"issue_date": "2026-06-13", "partner_name": "X", "account_item_name": "消耗品費", "amount": 100}
                )

    def test_cannot_delete_synced_deal(self) -> None:
        with app.db_connection() as conn:
            deal_id, _ = app.create_deal(conn, sample_deal())
            # 在庫連携の取引は削除不可（在庫ダッシュボードが正）。残っていること。
            self.assertFalse(app.delete_deal(conn, deal_id))
            self.assertIsNotNone(app.get_deal(conn, deal_id))

    def test_synced_deal_row_has_no_actions(self) -> None:
        with app.db_connection() as conn:
            app.create_deal(conn, sample_deal())
        html = app.render_index().decode("utf-8")
        # 在庫連携の行は編集・削除ボタンを出さず「在庫側で管理」と表示する。
        self.assertIn("在庫側で管理", html)
        self.assertNotIn("/edit", html)
        self.assertNotIn("/delete", html)

    def test_render_index_shows_deal_actions(self) -> None:
        with app.db_connection() as conn:
            app.create_manual_expense(
                conn,
                {"issue_date": "2026-06-13", "partner_name": "日本橋文具", "account_item_name": "消耗品費", "amount": 3300},
            )
        html = app.render_index().decode("utf-8")
        self.assertIn("<th>操作</th>", html)
        self.assertIn("/edit", html)
        self.assertIn("/delete", html)
        self.assertIn('class="row-del"', html)

    def test_edit_page_for_manual_and_not_for_synced(self) -> None:
        with app.db_connection() as conn:
            created = app.create_manual_expense(
                conn,
                {"issue_date": "2026-06-13", "partner_name": "日本橋文具", "account_item_name": "消耗品費", "amount": 3300},
            )
            synced_id, _ = app.create_deal(conn, sample_deal())
        manual_body = app.render_edit_deal(created["pseudo_freee_deal_id"])
        synced_body = app.render_edit_deal(synced_id)
        assert manual_body is not None
        self.assertIn("を編集", manual_body.decode("utf-8"))
        self.assertIsNone(synced_body)

    def test_expense_master_candidates_are_seeded_and_learn_new_values(self) -> None:
        with app.db_connection() as conn:
            before = app.list_expense_masters(conn)
            app.create_manual_expense(
                conn,
                {
                    "issue_date": "2026-06-13",
                    "partner_name": "新規テスト支払先",
                    "account_item_name": "新規テスト勘定",
                    "tax_category": "対象外",
                    "amount": 1200,
                },
            )
            after = app.list_expense_masters(conn)

        self.assertIn("消耗品費", before["account_items"])
        self.assertIn("課税仕入 10%", before["tax_categories"])
        self.assertIn("新規テスト支払先", after["payees"])
        self.assertIn("新規テスト勘定", after["account_items"])
        self.assertIn("対象外", after["tax_categories"])

    def test_create_expense_master_adds_account_item_with_default_tax(self) -> None:
        with app.db_connection() as conn:
            result = app.create_expense_master(
                conn,
                {
                    "master_type": "account_item",
                    "name": "研修費",
                    "default_tax_category": "課税仕入 10%",
                    "search_key": "kenshu",
                },
            )
            masters = app.list_expense_masters(conn)

        self.assertTrue(result["ok"])
        self.assertIn("研修費", masters["account_items"])
        setting = next(row for row in masters["account_item_settings"] if row["account_item_name"] == "研修費")
        self.assertEqual(setting["default_tax_category"], "課税仕入 10%")
        self.assertEqual(setting["search_key"], "kenshu")

    def test_create_expense_master_updates_payee_search_key(self) -> None:
        with app.db_connection() as conn:
            app.create_expense_master(
                conn,
                {
                    "master_type": "payee",
                    "name": "日本橋文具",
                    "search_key": "nb",
                },
            )
            masters = app.list_expense_masters(conn)

        setting = next(row for row in masters["payee_settings"] if row["payee_name"] == "日本橋文具")
        self.assertEqual(setting["search_key"], "nb")

    def test_seed_master_data_sets_default_search_keys_without_overwriting_existing_values(self) -> None:
        with app.db_connection() as conn:
            masters = app.list_expense_masters(conn)
            payee = next(row for row in masters["payee_settings"] if row["payee_name"] == "東京サプライ")
            account_item = next(row for row in masters["account_item_settings"] if row["account_item_name"] == "会議費")
            app.create_expense_master(conn, {"master_type": "account_item", "name": "会議費", "search_key": "meeting"})
            app.seed_master_data(conn)
            updated = app.list_expense_masters(conn)

        self.assertEqual(payee["search_key"], "tokyo")
        self.assertEqual(account_item["search_key"], "kai")
        updated_account_item = next(
            row for row in updated["account_item_settings"] if row["account_item_name"] == "会議費"
        )
        self.assertEqual(updated_account_item["search_key"], "meeting")

    def test_master_form_disables_tax_field_and_omits_tax_category_type(self) -> None:
        html = app.render_index().decode("utf-8")

        self.assertIn('<option value="payee">取引先</option>', html)
        self.assertIn('<option value="account_item">勘定科目</option>', html)
        self.assertNotIn('<option value="tax_category">税区分</option>', html)
        self.assertIn('input name="search_key"', html)
        self.assertIn("<label data-master-tax-field>標準税区分", html)
        self.assertIn('select name="default_tax_category" disabled', html)
        self.assertIn('data-master-submit>追加</button>', html)
        self.assertIn("<strong>取引先</strong>", html)
        self.assertIn("候補数: 取引先", html)
        self.assertIn('<select name="partner_query">', html)

    def test_render_index_uses_master_search_key_for_combo_search_and_selection(self) -> None:
        with app.db_connection() as conn:
            app.create_expense_master(conn, {"master_type": "payee", "name": "検索キー支払先", "search_key": "ks"})
            app.create_expense_master(
                conn,
                {
                    "master_type": "account_item",
                    "name": "検索キー勘定",
                    "search_key": "kk",
                    "default_tax_category": "対象外",
                },
            )

        html = app.render_index().decode("utf-8")

        self.assertIn('data-value="検索キー支払先" data-search="検索キー支払先 ks"', html)
        self.assertIn('data-value="検索キー勘定" data-search="検索キー勘定 kk"', html)
        self.assertIn('data-master-type="payee"', html)
        self.assertIn('data-name="検索キー支払先"', html)
        self.assertIn('data-search-key="ks"', html)
        self.assertIn('data-default-tax-category="対象外"', html)

    def test_list_deals_filters_by_partner_and_deal_type(self) -> None:
        with app.db_connection() as conn:
            app.create_deal(conn, sample_deal())
            app.create_manual_expense(
                conn,
                {
                    "issue_date": "2026-06-13",
                    "partner_name": "日本橋文具",
                    "account_item_name": "消耗品費",
                    "amount": 3300,
                },
            )
            deals = app.list_deals(conn, {"deal_type": "expense", "partner_query": "文具"})

        self.assertEqual(len(deals), 1)
        self.assertEqual(deals[0]["source_type"], "manual_expense")


class PseudoFreeeVoucherTest(unittest.TestCase):
    """A-5 ステップ2: レシートのAI読み取り（写真→下書き→人が登録）。"""

    def setUp(self) -> None:
        # 鍵を外す＝決定的スタブで解析（本物AIを呼ばない・課金しない）。在庫側テストと同じ方針。
        self._saved_api_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ.pop("ANTHROPIC_API_KEY", None)
        # A-8: .env に DATABASE_URL があっても、このテストは必ずローカル SQLite を使う。
        self._saved_db_url = os.environ.pop("DATABASE_URL", None)

        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = app.DB_PATH
        self.original_voucher_dir = app.VOUCHER_DIR
        app.DB_PATH = Path(self.tmp.name) / "pseudo_freee_test.db"
        app.VOUCHER_DIR = Path(self.tmp.name) / "voucher_store"
        app.init_db()

    def tearDown(self) -> None:
        app.DB_PATH = self.original_db_path
        app.VOUCHER_DIR = self.original_voucher_dir
        self.tmp.cleanup()
        if self._saved_api_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._saved_api_key
        if self._saved_db_url is not None:
            os.environ["DATABASE_URL"] = self._saved_db_url

    def test_capture_expense_returns_draft_and_saves_voucher_without_registering(self) -> None:
        with app.db_connection() as conn:
            result = app.capture_expense(
                conn, file_name="receipt.png", mime_type="image/png", image_bytes=b"demo-1"
            )
            vouchers = app.list_vouchers(conn)
            deals = app.list_deals(conn)

        self.assertTrue(result["ok"])
        # 鍵が無ければ決定的スタブ（お試しモード）。
        self.assertEqual(result["source"], "stub")
        self.assertIn("partner_name", result["draft"])
        self.assertIn("amount", result["draft"])
        self.assertGreater(result["voucher_id"], 0)
        # 証憑は保存されるが、未登録（登録は人）。
        self.assertEqual(len(vouchers), 1)
        self.assertFalse(vouchers[0]["registered"])
        self.assertIsNone(vouchers[0]["deal_id"])
        # 鉄則: AIは自動登録しない（取引は作られない）。
        self.assertEqual(len(deals), 0)

    def test_capture_expense_writes_original_image(self) -> None:
        with app.db_connection() as conn:
            captured = app.capture_expense(
                conn, file_name="r.png", mime_type="image/png", image_bytes=b"demo-2"
            )
            image = app.load_voucher_image(conn, captured["voucher_id"])

        self.assertIsNotNone(image)
        assert image is not None
        self.assertEqual(image[0], b"demo-2")
        self.assertEqual(image[1], "image/png")

    def test_manual_expense_with_voucher_id_links_voucher(self) -> None:
        with app.db_connection() as conn:
            captured = app.capture_expense(
                conn, file_name="r.png", mime_type="image/png", image_bytes=b"demo-3"
            )
            result = app.create_manual_expense(
                conn,
                {
                    "issue_date": "2026-06-13",
                    "partner_name": "日本橋文具",
                    "account_item_name": "消耗品費",
                    "tax_category": "課税仕入 10%",
                    "amount": 3300,
                    "voucher_id": captured["voucher_id"],
                },
            )
            voucher = app.voucher_detail(conn, captured["voucher_id"])

        assert voucher is not None
        self.assertTrue(voucher["registered"])
        self.assertEqual(voucher["deal_id"], result["pseudo_freee_deal_id"])

    def test_delete_voucher_removes_row_and_image(self) -> None:
        with app.db_connection() as conn:
            captured = app.capture_expense(
                conn, file_name="r.png", mime_type="image/png", image_bytes=b"demo-4"
            )
            row = app._voucher_row(conn, captured["voucher_id"])
            assert row is not None
            path = app.VOUCHER_DIR / row["storage_path"]
            self.assertTrue(path.exists())
            deleted = app.delete_voucher(conn, captured["voucher_id"])
            remaining = app.list_vouchers(conn)

        self.assertTrue(deleted)
        self.assertEqual(len(remaining), 0)
        self.assertFalse(path.exists())

    def test_delete_missing_voucher_returns_false(self) -> None:
        with app.db_connection() as conn:
            self.assertFalse(app.delete_voucher(conn, 999))

    def test_capture_same_image_twice_flags_duplicate(self) -> None:
        with app.db_connection() as conn:
            first = app.capture_expense(conn, file_name="r.png", mime_type="image/png", image_bytes=b"same-image")
            second = app.capture_expense(conn, file_name="r2.png", mime_type="image/png", image_bytes=b"same-image")
            other = app.capture_expense(conn, file_name="x.png", mime_type="image/png", image_bytes=b"different")

        # 1回目は重複なし。同じ画像の2回目は重複あり（1回目を指す）。別画像は重複なし。
        self.assertFalse(first["duplicate"])
        self.assertEqual(first["duplicate_of"], [])
        self.assertTrue(second["duplicate"])
        self.assertIn(first["voucher_id"], second["duplicate_of"])
        self.assertFalse(other["duplicate"])

    def test_backfill_voucher_hashes_enables_dup_detection(self) -> None:
        with app.db_connection() as conn:
            captured = app.capture_expense(conn, file_name="r.png", mime_type="image/png", image_bytes=b"old-receipt")
            # 修正前を模擬: content_hash を空に戻す（後から列を足した既存証憑の状態）。
            conn.execute("UPDATE pseudo_freee_vouchers SET content_hash = '' WHERE id = ?", (captured["voucher_id"],))
        with app.db_connection() as conn:
            app.backfill_voucher_hashes(conn)
            row = conn.execute(
                "SELECT content_hash FROM pseudo_freee_vouchers WHERE id = ?", (captured["voucher_id"],)
            ).fetchone()
            self.assertTrue(row["content_hash"])  # 画像から再計算して埋まる
        # 同じ画像を再アップロードすると、今度は重複検知される。
        with app.db_connection() as conn:
            again = app.capture_expense(conn, file_name="r2.png", mime_type="image/png", image_bytes=b"old-receipt")
        self.assertTrue(again["duplicate"])
        self.assertIn(captured["voucher_id"], again["duplicate_of"])

    def test_delete_deal_reverts_linked_voucher_to_draft(self) -> None:
        with app.db_connection() as conn:
            captured = app.capture_expense(conn, file_name="r.png", mime_type="image/png", image_bytes=b"dealvoucher")
            result = app.create_manual_expense(
                conn,
                {
                    "issue_date": "2026-06-13", "partner_name": "日本橋文具",
                    "account_item_name": "消耗品費", "amount": 3300, "voucher_id": captured["voucher_id"],
                },
            )
            self.assertTrue(app.voucher_detail(conn, captured["voucher_id"])["registered"])
            app.delete_deal(conn, result["pseudo_freee_deal_id"])
            voucher = app.voucher_detail(conn, captured["voucher_id"])
        # 取引を消しても証憑は残り、「下書き」に戻る。
        assert voucher is not None
        self.assertFalse(voucher["registered"])
        self.assertIsNone(voucher["deal_id"])

    def test_render_index_includes_ai_capture_ui(self) -> None:
        html = app.render_index().decode("utf-8")

        self.assertIn("レシートをAIで読み取る", html)
        self.assertIn('id="ai-dropzone"', html)
        self.assertIn('name="voucher_id"', html)
        self.assertIn('id="voucher-list"', html)
        self.assertIn("/api/expense-capture", html)
        # 追加UI: 大きいプレビュー・支払方法・支払予定日・取り消しボタン・重複警告・右カラム
        self.assertIn('id="receipt-preview-img"', html)
        self.assertIn('id="payment-method"', html)
        self.assertIn('id="due-date-input"', html)
        self.assertIn('id="ai-cancel"', html)
        self.assertIn('id="ai-dup-warning"', html)
        self.assertIn('class="right-col"', html)
        # 下部の並び順: 取引一覧 → 月次推移 → マスタ設定
        i_deals = html.find("<h2>取引一覧</h2>")
        i_trend = html.find("<h2>月次推移</h2>")
        i_master = html.find("<h2>マスタ設定</h2>")
        self.assertTrue(0 < i_deals < i_trend < i_master)


class PseudoFreeeAuthGateTest(unittest.TestCase):
    """A-6: 在庫アプリと同じ Clerk でサインインゲートを掛ける（同じログインで両アプリ）。"""

    _ENV_KEYS = ("CLERK_ISSUER", "CLERK_JWKS_URL", "CLERK_PUBLISHABLE_KEY", "APP_ENV", "AUTH_DEV_MODE")

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self._ENV_KEYS}
        for k in self._ENV_KEYS:
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_gate_active_when_clerk_configured_in_production(self) -> None:
        os.environ["APP_ENV"] = "production"
        os.environ["CLERK_ISSUER"] = "https://demo.clerk.accounts.dev"
        os.environ["CLERK_PUBLISHABLE_KEY"] = "pk_test_ZGVtby5jbGVyay5hY2NvdW50cy5kZXYk"
        html = app.render_page("テスト", "<p>本文</p>").decode("utf-8")
        # 本文が出る前に伏せるプリロード＋サインインゲートが入る。
        self.assertIn("pf-gated", html)
        self.assertIn('id="pf-signin-gate"', html)
        self.assertIn('"clerkConfigured": true', html)
        self.assertIn('"devMode": false', html)
        # 公開キーは出してよい（埋め込まれる）。秘密キーは扱っていない。
        self.assertIn("pk_test_ZGVtby5jbGVyay5hY2NvdW50cy5kZXYk", html)

    def test_dev_mode_passes_through_without_gate(self) -> None:
        os.environ["APP_ENV"] = "development"
        os.environ["AUTH_DEV_MODE"] = "true"
        os.environ["CLERK_ISSUER"] = "https://demo.clerk.accounts.dev"
        os.environ["CLERK_PUBLISHABLE_KEY"] = "pk_test_ZGVtby5jbGVyay5hY2NvdW50cy5kZXYk"
        html = app.render_page("テスト", "<p>本文</p>").decode("utf-8")
        # devMode=true なので、ブラウザ側スクリプトはゲートせず素通りする。
        self.assertIn('"devMode": true', html)

    def test_inventory_launcher_link_shown_when_env_set(self) -> None:
        # INVENTORY_APP_URL は import 時に読むので、定数を一時差し替えして検証する。
        original = app.INVENTORY_APP_URL
        try:
            app.INVENTORY_APP_URL = "https://inventory.example.com"
            html = app.render_page("テスト", "<p>本文</p>").decode("utf-8")
            self.assertIn("https://inventory.example.com/launcher", html)
            self.assertIn("アプリ入口へ", html)
        finally:
            app.INVENTORY_APP_URL = original


class PseudoFreeeProductionStartupTest(unittest.TestCase):
    """A-6: 本番(Render)は env PORT で 0.0.0.0 待受、ローカルは 127.0.0.1:8010。

    HOST/PORT は import 時に env から決まるため importlib.reload で読み直して検証する。
    ローカル .env の PSEUDO_FREEE_HOST/PORT に左右されないよう、関連 env を本テスト内で
    明示的に制御する（本番Renderでは PSEUDO_FREEE_HOST は無く PORT だけが渡される＝それを再現）。
    """

    _KEYS = ("PORT", "PSEUDO_FREEE_HOST", "PSEUDO_FREEE_PORT")

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self._KEYS}

    def tearDown(self) -> None:
        import importlib

        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(app)  # 既定状態へ戻す（後続テストに影響させない）

    def test_cloud_port_switches_host_to_all_interfaces(self) -> None:
        import importlib

        # 本番再現: PSEUDO_FREEE_HOST 無し（"" にして .env の値を無効化）＋ PORT のみ渡る。
        os.environ["PSEUDO_FREEE_HOST"] = ""
        os.environ["PORT"] = "10000"
        importlib.reload(app)
        self.assertEqual(app.PORT, 10000)
        self.assertEqual(app.HOST, "0.0.0.0")

    def test_local_defaults_to_loopback_8010(self) -> None:
        import importlib

        os.environ.pop("PORT", None)
        os.environ["PSEUDO_FREEE_HOST"] = "127.0.0.1"
        os.environ["PSEUDO_FREEE_PORT"] = "8010"
        importlib.reload(app)
        self.assertEqual(app.HOST, "127.0.0.1")
        self.assertEqual(app.PORT, 8010)


class PseudoFreeeByoKeyTest(unittest.TestCase):
    """A-8: BYO-key — 利用者のキーを解析にだけ使い、サーバに保存・記録しない（在庫と同じ方針）。"""

    def setUp(self) -> None:
        self._saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        self._saved_db = os.environ.pop("DATABASE_URL", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = app.DB_PATH
        self.original_voucher_dir = app.VOUCHER_DIR
        app.DB_PATH = Path(self.tmp.name) / "t.db"
        app.VOUCHER_DIR = Path(self.tmp.name) / "vs"
        app.init_db()

    def tearDown(self) -> None:
        app.DB_PATH = self.original_db_path
        app.VOUCHER_DIR = self.original_voucher_dir
        self.tmp.cleanup()
        if self._saved_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._saved_key
        if self._saved_db is not None:
            os.environ["DATABASE_URL"] = self._saved_db

    def test_api_key_override_takes_priority(self) -> None:
        import ai_capture

        self.assertEqual(ai_capture._api_key("sk-ant-override"), "sk-ant-override")
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-env"
        try:
            self.assertEqual(ai_capture._api_key("sk-ant-override"), "sk-ant-override")  # 利用者キー優先
            self.assertEqual(ai_capture._api_key(""), "sk-ant-env")                       # 無ければ env
        finally:
            os.environ.pop("ANTHROPIC_API_KEY", None)

    def test_capture_expense_forwards_api_key_to_analyzer(self) -> None:
        import ai_capture

        seen = {}
        original = ai_capture.analyze_voucher

        def fake(image_bytes, mime_type="", *, account_items=None, tax_categories=None, api_key=""):
            seen["api_key"] = api_key
            return original(image_bytes, mime_type, account_items=account_items, tax_categories=tax_categories)

        ai_capture.analyze_voucher = fake
        try:
            with app.db_connection() as conn:
                app.capture_expense(
                    conn, file_name="r.png", mime_type="image/png", image_bytes=b"x", api_key="sk-ant-user"
                )
        finally:
            ai_capture.analyze_voucher = original
        self.assertEqual(seen.get("api_key"), "sk-ant-user")

    def test_api_key_is_not_stored_on_voucher(self) -> None:
        import ai_capture

        # 解析はスタブにモック（ダミー鍵で実APIを叩かない）。鍵を渡しても証憑に残らないことを見る。
        original = ai_capture.analyze_voucher

        def fake(image_bytes, mime_type="", *, account_items=None, tax_categories=None, api_key=""):
            return original(image_bytes, mime_type, account_items=account_items, tax_categories=tax_categories)

        ai_capture.analyze_voucher = fake
        try:
            with app.db_connection() as conn:
                res = app.capture_expense(
                    conn, file_name="r.png", mime_type="image/png", image_bytes=b"y", api_key="sk-ant-secret"
                )
                row = app._voucher_row(conn, res["voucher_id"])
        finally:
            ai_capture.analyze_voucher = original
        self.assertNotIn("sk-ant-secret", str(row))  # 鍵は証憑に残らない


class PseudoFreeeBookkeepingTest(unittest.TestCase):
    """複式簿記（試算表・BS・PL・三分法の売上原価）— DOUBLE_ENTRY_BOOKKEEPING_PLAN.md Phase A。"""

    def setUp(self) -> None:
        self._saved_db_url = os.environ.pop("DATABASE_URL", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = app.DB_PATH
        app.DB_PATH = Path(self.tmp.name) / "pseudo_freee_test.db"
        app.init_db()

    def tearDown(self) -> None:
        app.DB_PATH = self.original_db_path
        self.tmp.cleanup()
        if self._saved_db_url is not None:
            os.environ["DATABASE_URL"] = self._saved_db_url

    def _add_sample_deals(self) -> None:
        """掛売上・現金仕入・手入力経費を 1 件ずつ入れる（残高に偏りを作る）。"""
        with app.db_connection() as conn:
            app.create_deal(
                conn,
                {
                    "queue_id": 1, "source_type": "sale", "source_id": 1,
                    "payload": {
                        "type": "income", "issue_date": "2026-03-10", "due_date": "2026-04-30",
                        "partner_name": "青山ECストア",
                        "details": [{"amount": 500000, "account_item_name": "売上高", "quantity": 1, "unit_price": 500000}],
                    },
                },
            )
            app.create_deal(
                conn,
                {
                    "queue_id": 2, "source_type": "purchase", "source_id": 2,
                    "payload": {
                        "type": "expense", "issue_date": "2026-03-05", "due_date": "",
                        "partner_name": "東京サプライ",
                        "details": [{"amount": 200000, "account_item_name": "仕入高", "quantity": 1, "unit_price": 200000}],
                    },
                },
            )
            app.create_manual_expense(
                conn,
                {"issue_date": "2026-03-15", "partner_name": "日本郵便", "account_item_name": "通信費",
                 "amount": 3000, "payment_method": "現金"},
            )

    def _entries(self, deal: dict) -> tuple[str, str, float, float]:
        entries = app.derive_journal_entries(deal)
        debit = sum(e["amount"] for e in entries if e["side"] == "借")
        credit = sum(e["amount"] for e in entries if e["side"] == "貸")
        debit_account = next(e["account"] for e in entries if e["side"] == "借")
        credit_account = next(e["account"] for e in entries if e["side"] == "貸")
        return debit_account, credit_account, debit, credit

    def test_journal_entries_balanced(self) -> None:
        # 各取引タイプで 借方合計 == 貸方合計、かつ正しい相手科目に展開される。
        cases = [
            # (deal, 期待借方, 期待貸方)
            ({"deal_type": "income", "source_type": "sale", "due_date": "2026-04-30", "amount": 500000}, "売掛金", "売上高"),
            ({"deal_type": "income", "source_type": "sale", "due_date": "", "amount": 500000}, "現金", "売上高"),
            ({"deal_type": "expense", "source_type": "purchase", "due_date": "2026-04-30", "amount": 200000}, "仕入高", "買掛金"),
            ({"deal_type": "expense", "source_type": "purchase", "due_date": "", "amount": 200000}, "仕入高", "現金"),
            ({"deal_type": "expense", "source_type": "manual_expense", "payment_method": "未払金", "account_item_name": "通信費", "amount": 3000}, "通信費", "未払金"),
            ({"deal_type": "expense", "source_type": "manual_expense", "payment_method": "現金", "account_item_name": "消耗品費", "amount": 1500}, "消耗品費", "現金"),
        ]
        for deal, expect_debit, expect_credit in cases:
            debit_account, credit_account, debit, credit = self._entries(deal)
            self.assertAlmostEqual(debit, credit, msg=f"unbalanced: {deal}")
            self.assertEqual(debit_account, expect_debit, msg=f"debit account: {deal}")
            self.assertEqual(credit_account, expect_credit, msg=f"credit account: {deal}")

    def test_opening_balance_balances(self) -> None:
        with app.db_connection() as conn:
            rows = app.opening_balance_rows(conn)
        debit = sum(float(r["amount"]) for r in rows if r["side"] == "借")
        credit = sum(float(r["amount"]) for r in rows if r["side"] == "貸")
        self.assertGreater(debit, 0)
        self.assertAlmostEqual(debit, credit)

    def test_trial_balance_debit_equals_credit(self) -> None:
        # 取引が無くても（決算整理のみ）、入れても、貸借が一致する。
        with app.db_connection() as conn:
            empty = app.calculate_trial_balance(conn)
        self.assertTrue(empty["balanced"])
        self.assertAlmostEqual(empty["debit_total"], empty["credit_total"])

        self._add_sample_deals()
        with app.db_connection() as conn:
            trial = app.calculate_trial_balance(conn)
        self.assertTrue(trial["balanced"])
        self.assertAlmostEqual(trial["debit_total"], trial["credit_total"])
        # 決算整理後は仕入高は売上原価へ振り替えられ、試算表に残らない。
        self.assertNotIn("仕入高", [r["account"] for r in trial["rows"]])

    def test_balance_sheet_balances(self) -> None:
        self._add_sample_deals()
        with app.db_connection() as conn:
            sheet = app.calculate_balance_sheet(conn)
        self.assertTrue(sheet["balanced"])
        self.assertAlmostEqual(sheet["asset_total"], sheet["liabilities_equity_total"])

    def test_net_income_flows_to_equity(self) -> None:
        self._add_sample_deals()
        with app.db_connection() as conn:
            income = app.calculate_income_statement(conn)
            sheet = app.calculate_balance_sheet(conn)
        # PL の当期純利益が BS 純資産に独立行として現れ、資産＝負債＋純資産が保たれる。
        equity_accounts = {row["account"]: row["amount"] for row in sheet["equity"]}
        self.assertIn("当期純利益", equity_accounts)
        self.assertAlmostEqual(equity_accounts["当期純利益"], income["net_income"])
        self.assertAlmostEqual(sheet["asset_total"], sheet["liabilities_equity_total"])

    def test_cancel_deal_offsets_sale(self) -> None:
        # Phase C: マイナス金額の取消 deal を 1 本入れるだけで、KPI・PL・残高が元仕訳を相殺する。
        with app.db_connection() as conn:
            app.create_deal(
                conn,
                {
                    "queue_id": 10, "source_type": "sale", "source_id": 7,
                    "payload": {
                        "type": "income", "issue_date": "2026-03-10", "due_date": "2026-04-30",
                        "partner_name": "青山ECストア",
                        "details": [{"amount": 300000, "account_item_name": "売上高", "quantity": 1, "unit_price": 300000}],
                    },
                },
            )
            before_income = app.calculate_income_statement(conn)
            before_balances = app.account_balances(conn)

            # 取消仕訳: 元と同じ source_type/source_id/due_date だが queue_id は別、金額はマイナス。
            app.create_deal(
                conn,
                {
                    "queue_id": 11, "source_type": "sale", "source_id": 7,
                    "payload": {
                        "type": "income", "issue_date": "2026-03-10", "due_date": "2026-04-30",
                        "partner_name": "青山ECストア", "memo": "取消: ORD-7",
                        "details": [{"amount": -300000, "account_item_name": "売上高", "quantity": -1, "unit_price": 300000}],
                    },
                },
            )
            after_income = app.calculate_income_statement(conn)
            after_balances = app.account_balances(conn)
            deals = app.list_deals(conn)

        # 元仕訳＋取消仕訳の両方が残る（監査証跡）。queue_id 違いで重複保存される。
        self.assertEqual(len(deals), 2)
        # 売上高・売掛金が、取消で元の水準（取引なし＝0）まで戻る。
        self.assertAlmostEqual(after_income["sales"], before_income["sales"] - 300000)
        self.assertAlmostEqual(after_income["sales"], 0.0)
        self.assertAlmostEqual(after_balances.get("売掛金", 0.0), before_balances.get("売掛金", 0.0) - 300000)

    def test_render_index_shows_cancel_badge_for_negative_deal(self) -> None:
        # Phase C: マイナス金額の取消 deal には「取消」バッジが付く（見える化）。
        with app.db_connection() as conn:
            app.create_deal(
                conn,
                {
                    "queue_id": 9, "source_type": "purchase", "source_id": 3,
                    "payload": {
                        "type": "expense", "issue_date": "2026-06-12", "due_date": "2026-07-31",
                        "partner_name": "東京サプライ", "memo": "取消: P-1",
                        "details": [{"amount": -13475, "account_item_name": "仕入高", "quantity": -25, "unit_price": 539}],
                    },
                },
            )
        html = app.render_index().decode("utf-8")
        self.assertIn('<span class="badge expense">取消</span>', html)

    def test_cogs_three_split(self) -> None:
        # 売上原価 = 期首商品 + 当期仕入 − 期末商品（三分法）。
        self._add_sample_deals()
        with app.db_connection() as conn:
            beginning = app.opening_inventory_amount(conn)
            purchases = app.current_period_purchases(conn)
            ending = app.closing_inventory_physical_amount(conn)
            income = app.calculate_income_statement(conn)
            balances = app.account_balances(conn)
        self.assertAlmostEqual(income["cogs"], beginning + purchases - ending)
        # 決算整理後: 仕入高は 0、商品は期末棚卸高になる。
        self.assertAlmostEqual(balances.get("仕入高", 0.0), 0.0)
        self.assertAlmostEqual(balances.get("商品", 0.0), ending)

    def test_closing_inventory_override_changes_cogs_and_inventory(self) -> None:
        # 期末棚卸高を手入力で上書きすると、売上原価と BS の商品が連動する。
        self._add_sample_deals()
        with app.db_connection() as conn:
            conn.execute(
                "UPDATE pseudo_freee_closing_inventory SET physical_amount = ? WHERE period = ?",
                (120000, app.DEFAULT_CLOSING_INVENTORY[0]),
            )
        with app.db_connection() as conn:
            income = app.calculate_income_statement(conn)
            sheet = app.calculate_balance_sheet(conn)
            beginning = app.opening_inventory_amount(conn)
            purchases = app.current_period_purchases(conn)
        self.assertAlmostEqual(income["cogs"], beginning + purchases - 120000)
        inventory_amount = next(row["amount"] for row in sheet["assets"] if row["account"] == "商品")
        self.assertAlmostEqual(inventory_amount, 120000)
        self.assertTrue(sheet["balanced"])

    def test_shrinkage_loss_when_book_exceeds_physical(self) -> None:
        # 棚卸減耗: 帳簿>実地のとき、棚卸減耗損を計上し売上原価へ算入する（原価性）。
        self._add_sample_deals()
        with app.db_connection() as conn:
            app.upsert_closing_inventory(conn, {"period": "209912", "book_amount": 200000, "physical_amount": 180000})
        with app.db_connection() as conn:
            beginning = app.opening_inventory_amount(conn)
            purchases = app.current_period_purchases(conn)
            journal = app.closing_journal(conn)
            balances = app.account_balances(conn)
            income = app.calculate_income_statement(conn)
            sheet = app.calculate_balance_sheet(conn)
        descs = [t["description"] for t in journal]
        self.assertIn("棚卸減耗損の計上（帳簿−実地）", descs)
        self.assertIn("棚卸減耗損を売上原価へ算入", descs)
        # 棚卸減耗損は売上原価へ振り替えられ残高 0（独立PL行は出ない＝原価性）。
        self.assertAlmostEqual(balances.get("棚卸減耗損", 0.0), 0.0)
        # 売上原価は実地ベース、BS の商品は実地額、貸借一致。
        self.assertAlmostEqual(income["cogs"], beginning + purchases - 180000)
        merchandise = next(row["amount"] for row in sheet["assets"] if row["account"] == "商品")
        self.assertAlmostEqual(merchandise, 180000)
        self.assertTrue(sheet["balanced"])
        # 決算ページに棚卸減耗損の行が出る。
        html = app.render_statements().decode("utf-8")
        self.assertIn("棚卸減耗損", html)

    def test_statements_page_renders(self) -> None:
        self._add_sample_deals()
        html = app.render_statements().decode("utf-8")
        self.assertIn("決算書（試算表・貸借対照表・損益計算書）", html)
        self.assertIn("損益計算書", html)
        self.assertIn("貸借対照表", html)
        self.assertIn("試算表", html)
        self.assertIn("売上原価の計算（三分法）", html)
        self.assertIn("貸借一致 ✓", html)

    def test_structural_accounts_excluded_from_expense_combo(self) -> None:
        # BS科目（現金・売掛金…）と売上高/売上原価/減価償却費は経費の勘定候補から外す。費用科目は残す。
        with app.db_connection() as conn:
            masters = app.list_expense_masters(conn)
        self.assertIn("消耗品費", masters["account_items"])
        self.assertIn("仕入高", masters["account_items"])
        for hidden in ("現金", "普通預金", "売掛金", "買掛金", "資本金", "売上高", "売上原価", "減価償却費", "減価償却累計額"):
            self.assertNotIn(hidden, masters["account_items"])

    def test_depreciation_indirect_method(self) -> None:
        # 減価償却（定額法・間接法）: 決算整理仕訳に 借)減価償却費 / 貸)減価償却累計額。
        self._add_sample_deals()
        with app.db_connection() as conn:
            journal = app.closing_journal(conn)
            sheet = app.calculate_balance_sheet(conn)
            income = app.calculate_income_statement(conn)
            dep = app.depreciation_amount(conn)
        dep_txn = next((t for t in journal if "減価償却" in t["description"]), None)
        self.assertIsNotNone(dep_txn)
        debit = next(e for e in dep_txn["entries"] if e["side"] == "借")
        credit = next(e for e in dep_txn["entries"] if e["side"] == "貸")
        self.assertEqual(debit["account"], "減価償却費")
        self.assertEqual(credit["account"], "減価償却累計額")
        # 間接法: 累計額は資産のマイナス（評価勘定）として BS 資産にマイナス表示。
        accumulated = next(x["amount"] for x in sheet["assets"] if x["account"] == "減価償却累計額")
        self.assertAlmostEqual(accumulated, -dep)
        # 減価償却費は PL の費用に入り、当期純利益を押し下げる。BS は一致を保つ。
        self.assertIn("減価償却費", [x["account"] for x in income["other_expenses"]])
        self.assertTrue(sheet["balanced"])

    def test_save_closing_procedure_updates_inventory_and_depreciation(self) -> None:
        self._add_sample_deals()
        with app.db_connection() as conn:
            app.save_closing_procedure(conn, {"period": "202603", "physical_amount": 120000, "depreciation_amount": 50000})
        with app.db_connection() as conn:
            beginning = app.opening_inventory_amount(conn)
            purchases = app.current_period_purchases(conn)
            income = app.calculate_income_statement(conn)
            sheet = app.calculate_balance_sheet(conn)
        self.assertAlmostEqual(app_inventory := next(x["amount"] for x in sheet["assets"] if x["account"] == "商品"), 120000)
        self.assertAlmostEqual(next(x["amount"] for x in sheet["assets"] if x["account"] == "減価償却累計額"), -50000)
        self.assertAlmostEqual(income["cogs"], beginning + purchases - 120000)
        self.assertTrue(sheet["balanced"])

    def test_save_closing_procedure_rejects_negative(self) -> None:
        with app.db_connection() as conn:
            with self.assertRaises(ValueError):
                app.save_closing_procedure(conn, {"period": "202603", "physical_amount": -1, "depreciation_amount": 0})

    def test_journal_transactions_each_balanced(self) -> None:
        # 仕訳帳: 開始記入＋期中取引＋決算整理。どの取引も借方合計＝貸方合計。
        self._add_sample_deals()
        with app.db_connection() as conn:
            transactions = app.journal_transactions(conn)
        kinds = {t["kind"] for t in transactions}
        self.assertEqual(kinds, {"opening", "deal", "closing"})
        for txn in transactions:
            debit = sum(e["amount"] for e in txn["entries"] if e["side"] == "借")
            credit = sum(e["amount"] for e in txn["entries"] if e["side"] == "貸")
            self.assertAlmostEqual(debit, credit, msg=f"unbalanced txn: {txn['description']}")

    def test_general_ledger_matches_account_balances(self) -> None:
        # 総勘定元帳の各科目の最終残高（借方プラス符号）は account_balances と一致し、総和は 0。
        self._add_sample_deals()
        with app.db_connection() as conn:
            ledger = app.general_ledger(conn)
            balances = app.account_balances(conn)
        total = 0.0
        for account in ledger:
            self.assertAlmostEqual(account["balance"], balances[account["account"]], msg=account["account"])
            total += account["balance"]
        self.assertAlmostEqual(total, 0.0)

    def test_statements_page_has_three_view_tabs_and_print(self) -> None:
        self._add_sample_deals()
        html = app.render_statements().decode("utf-8")
        # 3つのビュータブ。
        for marker in ('data-view-btn="statements"', 'data-view-btn="journal"', 'data-view-btn="ledger"'):
            self.assertIn(marker, html)
        # 各ビューのコンテナ。
        for marker in ('data-view="statements"', 'data-view="journal"', 'data-view="ledger"'):
            self.assertIn(marker, html)
        for marker in ("決算手続き（入力）", "決算整理仕訳", "仕訳帳", "総勘定元帳", "window.print()", "減価償却費"):
            self.assertIn(marker, html)
        self.assertIn('action="/closing"', html)
        # 総勘定元帳: 勘定科目セレクタ＋相手勘定の列。
        self.assertIn('id="ledger-account"', html)
        self.assertIn("相手勘定", html)
        self.assertIn("data-ledger-account=", html)

    def test_statements_active_view_controls_initial_visibility(self) -> None:
        self._add_sample_deals()
        for view in ("statements", "journal", "ledger"):
            html = app.render_statements(view).decode("utf-8")
            self.assertIn(f'window.__STMT_VIEW__ = "{view}";', html)
        # 不正な値は決算書にフォールバック。
        self.assertIn('window.__STMT_VIEW__ = "statements";', app.render_statements("bogus").decode("utf-8"))

    def test_general_ledger_includes_counter_account(self) -> None:
        # 現金の元帳に、相手勘定（仕入の相手＝仕入高 や 売上の相手＝売上高）が入る。諸口も使われる。
        self._add_sample_deals()
        with app.db_connection() as conn:
            ledger = {a["account"]: a for a in app.general_ledger(conn)}
        counters = {row["counter"] for row in ledger["現金"]["rows"]}
        # 期首（諸口）＋ 現金仕入の相手（仕入高）＋ 手入力経費の相手（通信費）。
        self.assertIn("諸口", counters)
        self.assertIn("仕入高", counters)

    def test_ledger_month_filter_and_carry_forward(self) -> None:
        # 2か月にまたがる現金売上。月で絞ると、その月の記入＋月初の前月繰越が出る。
        with app.db_connection() as conn:
            app.create_deal(conn, {"queue_id": 1, "source_type": "sale", "source_id": 1, "payload": {
                "type": "income", "issue_date": "2026-03-10", "due_date": "", "partner_name": "現金客",
                "details": [{"amount": 100000, "account_item_name": "売上高", "quantity": 1, "unit_price": 100000}]}})
            app.create_deal(conn, {"queue_id": 2, "source_type": "sale", "source_id": 2, "payload": {
                "type": "income", "issue_date": "2026-04-12", "due_date": "", "partner_name": "現金客",
                "details": [{"amount": 50000, "account_item_name": "売上高", "quantity": 1, "unit_price": 50000}]}})
        with app.db_connection() as conn:
            periods = app.ledger_periods(conn)
            full = {a["account"]: a for a in app.general_ledger(conn)}
            april = {a["account"]: a for a in app.general_ledger(conn, "2026-04")}
        self.assertIn("2026-03", periods)
        self.assertIn("2026-04", periods)
        self.assertIn("決算", periods)
        # 4月の現金: 月初繰越（期首30万＋3月10万＝40万）＋4月の+5万 → 残高45万。
        cash = april["現金"]
        self.assertAlmostEqual(cash["carry_forward"], 400000)
        self.assertEqual(len(cash["rows"]), 1)
        self.assertAlmostEqual(cash["balance"], 450000)
        # 全期間では繰越行は無し（期首から積む）。
        self.assertIsNone(full["現金"]["carry_forward"])
        # 4月に動きのない科目（備品）はその月の元帳に出ない。
        self.assertNotIn("備品", april)

    def test_statements_ledger_has_month_selector(self) -> None:
        self._add_sample_deals()
        html = app.render_statements("ledger").decode("utf-8")
        self.assertIn('id="ledger-month"', html)
        self.assertIn("全期間", html)
        # _add_sample_deals は 2026-03。月の選択肢に出る。
        self.assertIn(">2026-03<", html)
        # 月指定で初期表示・繰越が出る。
        html_april = app.render_statements("ledger", "2026-03").decode("utf-8")
        self.assertIn("前月繰越", html_april)

    def test_edit_target_rules(self) -> None:
        self.assertEqual(app._edit_target("deal", "manual_expense", 5), ("/deals/5/edit", "編集"))
        self.assertEqual(app._edit_target("deal", "purchase", 7)[0], "")  # 在庫連携は編集不可
        self.assertEqual(app._edit_target("deal", "sale", 7)[1], "在庫側で管理")
        self.assertEqual(app._edit_target("closing", None, None)[0], "/statements?view=statements#closing-form")
        self.assertEqual(app._edit_target("opening", None, None), ("", ""))

    def test_journal_and_ledger_have_edit_links(self) -> None:
        self._add_sample_deals()
        with app.db_connection() as conn:
            manual_id = app.list_deals(conn, {"source_type": "manual_expense"})[0]["id"]
        journal_html = app.render_statements("journal").decode("utf-8")
        self.assertIn(f"/deals/{manual_id}/edit", journal_html)  # 手入力経費は編集できる
        self.assertIn("在庫側で管理", journal_html)               # 在庫連携は編集不可ラベル
        self.assertIn("決算手続きで編集", journal_html)            # 決算整理は決算手続きへ
        self.assertIn('<th class="no-print">操作</th>', journal_html)
        ledger_html = app.render_statements("ledger").decode("utf-8")
        self.assertIn(f"/deals/{manual_id}/edit", ledger_html)
        self.assertIn("在庫側で管理", ledger_html)
        # 決算手続きフォームへのアンカー先が存在する。
        self.assertIn('id="closing-form"', app.render_statements("statements").decode("utf-8"))

    def test_index_shows_statements_buttons(self) -> None:
        with app.db_connection() as conn:
            app.create_manual_expense(
                conn, {"issue_date": "2026-06-13", "partner_name": "日本橋文具", "account_item_name": "消耗品費", "amount": 3300}
            )
        html = app.render_index().decode("utf-8")
        self.assertIn("決算書を表示", html)
        self.assertIn("仕訳帳を表示", html)
        self.assertIn("総勘定元帳を表示", html)
        self.assertIn('href="/statements?view=ledger"', html)


if __name__ == "__main__":
    unittest.main()
