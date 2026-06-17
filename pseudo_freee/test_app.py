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
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = app.DB_PATH
        app.DB_PATH = Path(self.tmp.name) / "pseudo_freee_test.db"
        app.init_db()

    def tearDown(self) -> None:
        app.DB_PATH = self.original_db_path
        self.tmp.cleanup()

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


if __name__ == "__main__":
    unittest.main()
