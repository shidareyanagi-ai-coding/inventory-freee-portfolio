"""疑似freee の Postgres バックエンド スモークテスト（A-8 永続化）。

DATABASE_URL が postgres を指すときだけ実行する（未設定ならスキップ）。
方言差を吸収した経路が実際に Postgres 上で動くことを確認する:
  - IDENTITY 採番 + INSERT ... RETURNING id（db.insert_returning_id）
  - ON CONFLICT DO NOTHING（マスタ再シード）/ ON CONFLICT(col) DO UPDATE（マスタ編集）
  - created_at を TEXT(to_char) で返す → stdlib json.dumps が壊れない
  - substr による月抽出（get_summary / get_monthly_trends）

⚠️ テストDBの分離（必須ルール / EVOLUTION_PLAN.md「テストDBの分離」）:
  本テストは対象DBの全テーブルを DROP→再作成する。本番Neon（実データ）に向けたまま
  走らせると消える。事故防止のため「DATABASE_URL がテスト用DBを指す」かつ
  「PYTEST_ALLOW_DB_RESET=1 を明示」したときだけ実行する（どちらか欠けると skip）。

検証例（疑似freee 用のテスト Neon を用意して）:
  $env:DATABASE_URL = "postgresql://...:.../pseudo_freee_test?sslmode=require"
  $env:PYTEST_ALLOW_DB_RESET = "1"
  python -m pytest test_postgres.py -q
"""

import os
import tempfile
import unittest
from pathlib import Path

import app
import db

DATABASE_URL = os.environ.get("DATABASE_URL", "")
RUN_PG = DATABASE_URL.startswith("postgres")
ALLOW_RESET = os.environ.get("PYTEST_ALLOW_DB_RESET", "").strip().lower() in {"1", "true", "yes", "on"}
PG_READY = RUN_PG and ALLOW_RESET
SKIP_REASON = (
    "DATABASE_URL を本番と別のテスト用DBに向け、PYTEST_ALLOW_DB_RESET=1 を"
    "設定したときのみ実行（本番Neonでの DROP 事故防止）"
)


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


@unittest.skipUnless(PG_READY, SKIP_REASON)
class PseudoFreeePostgresTest(unittest.TestCase):
    def setUp(self) -> None:
        # 鍵を外す＝決定的スタブで解析（本物AIを呼ばない・課金しない）。
        self._saved_api_key = os.environ.pop("ANTHROPIC_API_KEY", None)
        # 毎回まっさらなスキーマから（テスト用DBを想定）。
        with db.get_conn() as conn:
            db.reset_tables(conn)
        app.init_db()
        # 証憑画像はローカルの一時ディレクトリへ（リポジトリ/実R2に残さない。STORAGE_* は conftest が空固定）。
        self._original_voucher_dir = app.VOUCHER_DIR
        self.tmp = tempfile.TemporaryDirectory()
        app.VOUCHER_DIR = Path(self.tmp.name)

    def tearDown(self) -> None:
        app.VOUCHER_DIR = self._original_voucher_dir
        self.tmp.cleanup()
        if self._saved_api_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._saved_api_key

    def test_init_seeds_masters(self) -> None:
        with app.db_connection() as conn:
            masters = app.list_expense_masters(conn)
        self.assertIn("東京サプライ", masters["payees"])
        self.assertIn("仕入高", masters["account_items"])
        self.assertTrue(masters["tax_categories"])

    def test_create_deal_persists_and_dedupes(self) -> None:
        with app.db_connection() as conn:
            deal_id, created = app.create_deal(conn, sample_deal())
        self.assertTrue(created)
        self.assertIsInstance(deal_id, int)
        # 同一キューの再送は重複登録しない（ON CONFLICT/事前SELECT）。
        with app.db_connection() as conn:
            again_id, again_created = app.create_deal(conn, sample_deal())
        self.assertEqual(again_id, deal_id)
        self.assertFalse(again_created)
        # 取得できる＝再起動後も残る形で保存されている。
        with app.db_connection() as conn:
            deal = app.get_deal(conn, deal_id)
        self.assertIsNotNone(deal)
        self.assertEqual(deal["partner_name"], "東京サプライ")
        self.assertTrue(deal["lines"])
        # created_at は文字列（stdlib json.dumps が扱える）。
        self.assertIsInstance(deal["created_at"], str)

    def test_manual_expense_persists(self) -> None:
        with app.db_connection() as conn:
            result = app.create_manual_expense(
                conn,
                {
                    "issue_date": "2026-06-15",
                    "partner_name": "日本郵便",
                    "account_item_name": "通信費",
                    "tax_category": "課税仕入 10%",
                    "amount": 840,
                    "memo": "切手",
                    "payment_method": "現金",
                },
            )
            deals = app.list_deals(conn)
        self.assertIn(result["pseudo_freee_deal_id"], [d["id"] for d in deals])

    def test_capture_expense_persists_voucher(self) -> None:
        with app.db_connection() as conn:
            captured = app.capture_expense(
                conn, file_name="r.png", mime_type="image/png", image_bytes=b"demo-pg"
            )
            vouchers = app.list_vouchers(conn)
            image = app.load_voucher_image(conn, captured["voucher_id"])
        self.assertIn(captured["voucher_id"], [v["id"] for v in vouchers])
        self.assertIsNotNone(image)
        self.assertEqual(image[0], b"demo-pg")

    def test_summary_and_trends_run(self) -> None:
        with app.db_connection() as conn:
            app.create_deal(conn, sample_deal())
            summary = app.get_summary(conn)
            trends = app.get_monthly_trends(conn)
        self.assertIn("deal_count", summary)
        self.assertIsInstance(trends, list)


if __name__ == "__main__":
    unittest.main()
