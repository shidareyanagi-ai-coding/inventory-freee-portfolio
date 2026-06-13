from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
