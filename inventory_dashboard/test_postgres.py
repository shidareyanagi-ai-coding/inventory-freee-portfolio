"""Postgres バックエンドのスモークテスト（EVOLUTION_PLAN.md A-2/A-3）。

DATABASE_URL が postgres を指すときだけ実行する（未設定ならスキップ）。
方言差を吸収した経路が実際に動くことを Postgres 上で確認する:
  - IDENTITY 採番 + INSERT ... RETURNING id（db.insert_returning_id）
  - EXTRACT による月抽出（db.month_expr / forecast_simulation）
  - ON CONFLICT DO NOTHING / DO UPDATE（取引先・freeeキュー）
  - psycopg.IntegrityError → {"error": ...} 400 への整形
  - A-3: organization_id 絞り込み・dev モード認証経由のルーティング

⚠️ テストDBの分離（A-3 必須ルール / EVOLUTION_PLAN.md「テストDBの分離」）:
  本テストは対象DBの全ドメインテーブルを DROP→再作成する。本番Neon（実データ）に
  向けたまま走らせると消える。事故防止のため、destructive テストは
  「DATABASE_URL がテスト用ブランチ等を指す」かつ「PYTEST_ALLOW_DB_RESET=1 を明示」した
  ときだけ実行する（どちらか欠けると skip）。

ローカル検証例（throwaway Postgres）:
  docker run -d --name a3pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
  $env:DATABASE_URL = "postgresql://postgres:pw@127.0.0.1:55432/postgres"
  $env:PYTEST_ALLOW_DB_RESET = "1"
  python -m pytest test_postgres.py -q
"""

import json
import os
import unittest
from unittest.mock import patch

import app
import db

DATABASE_URL = os.environ.get("DATABASE_URL", "")
RUN_PG = DATABASE_URL.startswith("postgres")
ALLOW_RESET = os.environ.get("PYTEST_ALLOW_DB_RESET", "").strip().lower() in {"1", "true", "yes", "on"}
# 本番Neon誤爆を防ぐため、明示の opt-in が無ければ Postgres テストは実行しない。
PG_READY = RUN_PG and ALLOW_RESET
SKIP_REASON = (
    "DATABASE_URL を本番と別のテスト用ブランチ等に向け、PYTEST_ALLOW_DB_RESET=1 を"
    "設定したときのみ実行（本番Neonでの DROP 事故防止）"
)


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


@unittest.skipUnless(PG_READY, SKIP_REASON)
class PostgresSmokeTest(unittest.TestCase):
    def setUp(self):
        # 毎回まっさらなスキーマから（テスト用DBを想定）。
        with db.get_conn() as conn:
            db.reset_domain_tables(conn)
        app.init_db()
        # A-3: 業務ロジックは organization_id 必須。テスト用の組織を作り seed を入れる。
        with app.get_conn() as conn:
            self.org_id = app.create_organization(conn, "PGテスト組織")
            app.seed_organization(conn, self.org_id)

    def _first_product(self, conn):
        return conn.execute(
            "SELECT * FROM products WHERE organization_id = ? ORDER BY id LIMIT 1",
            (self.org_id,),
        ).fetchone()

    def test_backend_is_postgres(self):
        with app.get_conn() as conn:
            self.assertTrue(conn.postgres)

    def test_seed_reproduced_on_postgres(self):
        with app.get_conn() as conn:
            products = conn.execute(
                "SELECT * FROM products WHERE organization_id = ? ORDER BY id", (self.org_id,)
            ).fetchall()
            partners = app.list_business_partners(conn, self.org_id)
            demo_sales = conn.execute(
                "SELECT COUNT(*) AS count FROM sales WHERE organization_id = ? AND invoice_no LIKE 'DEMO-HIST-S-%'",
                (self.org_id,),
            ).fetchone()["count"]
        self.assertEqual(len(products), 3)
        self.assertIn("東京サプライ", partners["suppliers"])
        self.assertIn("青山ECストア", partners["customers"])
        self.assertGreater(demo_sales, 0)

    def test_purchase_returns_id_and_increases_stock(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            before = app.stock_by_product(conn, self.org_id)[product["id"]]
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "PGテスト仕入先",
                "invoice_no": "INV-PG-001",
                "quantity": 5,
                "unit_price": 1000,
            })
            after = app.stock_by_product(conn, self.org_id)[product["id"]]
            queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
        self.assertIsInstance(result["purchase_id"], int)
        self.assertEqual(after, before + 5)
        self.assertEqual(queue["status"], "pending")

    def test_oversell_raises(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            current = app.stock_by_product(conn, self.org_id)[product["id"]]
            with self.assertRaises(ValueError):
                app.create_sale(conn, self.org_id, {
                    "product_id": product["id"],
                    "partner_name": "PGテスト得意先",
                    "invoice_no": "ORD-PG-OVER",
                    "quantity": current + 1,
                    "unit_price": 1500,
                })

    def test_cancel_marks_queue_failed(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "PGテスト仕入先",
                "invoice_no": "INV-PG-CANCEL",
                "quantity": 2,
                "unit_price": 1000,
            })
            movement = conn.execute(
                "SELECT * FROM inventory_movements WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
            app.cancel_inventory_movement(conn, self.org_id, {"movement_id": movement["id"], "reason": "商品選択ミス"})
            queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
        self.assertEqual(queue["status"], "failed")
        self.assertIn("商品選択ミス", queue["sync_error_message"])

    def test_send_queue_marks_sent(self):
        def fake_urlopen(request, timeout):
            return FakeResponse({"ok": True, "pseudo_freee_deal_id": 202, "duplicate": False})

        with app.get_conn() as conn:
            product = self._first_product(conn)
            result = app.create_purchase(conn, self.org_id, {
                "product_id": product["id"],
                "partner_name": "PGテスト仕入先",
                "invoice_no": "INV-PG-SEND",
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
        self.assertEqual(send_result["external_accounting_id"], "pseudo-freee-202")
        self.assertEqual(updated["status"], "sent")

    def test_forecast_uses_extract_month(self):
        # forecast_simulation は monthly_seasonal_factor 経由で EXTRACT(MONTH ...) を実行する。
        with app.get_conn() as conn:
            result = app.forecast_simulation(conn, self.org_id, 30)
        self.assertEqual(result["horizon_days"], 30)
        self.assertEqual(len(result["rows"]), 3)
        self.assertIn("required_inventory", result["rows"][0])

    def test_product_ledger_newest_first(self):
        with app.get_conn() as conn:
            product = self._first_product(conn)
            ledger = app.product_ledger(conn, self.org_id, product["id"])["ledger"]
        dates = [row["movement_date"] for row in ledger]
        self.assertEqual(dates, sorted(dates, reverse=True))


@unittest.skipUnless(PG_READY, SKIP_REASON)
class PostgresRouteTest(unittest.TestCase):
    """TestClient 経由でルーティング層が Postgres でも旧契約どおり動くか確認する。

    A-3: 全 API は認証必須。ここでは AUTH_DEV_MODE=true + X-Dev-User-Id で擬似ログインする。
    """

    DEV_USER = "pg-route-user"

    def setUp(self):
        from fastapi.testclient import TestClient

        self._saved_env = {k: os.environ.get(k) for k in ("AUTH_DEV_MODE", "APP_ENV")}
        os.environ["AUTH_DEV_MODE"] = "true"
        os.environ["APP_ENV"] = "development"

        with db.get_conn() as conn:
            db.reset_domain_tables(conn)
        self.client_cm = TestClient(app.app)  # lifespan で init_db()
        self.client = self.client_cm.__enter__()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _h(self):
        return {"X-Dev-User-Id": self.DEV_USER}

    def test_dashboard_ok(self):
        res = self.client.get("/api/dashboard", headers=self._h())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["products"]), 3)

    def test_create_purchase_201(self):
        product = self.client.get("/api/products", headers=self._h()).json()[0]
        res = self.client.post("/api/purchases", headers=self._h(), json={
            "product_id": product["id"],
            "partner_name": "PGルート仕入先",
            "invoice_no": "INV-PG-ROUTE",
            "quantity": 3,
            "unit_price": 1000,
        })
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.json()["ok"])

    def test_oversell_400(self):
        product = self.client.get("/api/products", headers=self._h()).json()[0]
        res = self.client.post("/api/sales", headers=self._h(), json={
            "product_id": product["id"],
            "partner_name": "PGルート得意先",
            "invoice_no": "ORD-PG-ROUTE-OVER",
            "quantity": product["stock_quantity"] + 1,
            "unit_price": 1500,
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("在庫不足", res.json()["error"])

    def test_duplicate_sku_integrity_error_is_400(self):
        # UNIQUE(organization_id, sku) 違反 → psycopg.IntegrityError → 例外ハンドラで 400。
        body = {"sku": "SKU-PG-DUP", "product_name": "重複SKU商品"}
        first = self.client.post("/api/products", headers=self._h(), json=body)
        self.assertEqual(first.status_code, 201)
        second = self.client.post("/api/products", headers=self._h(), json=body)
        self.assertEqual(second.status_code, 400)
        self.assertIn("integrity", second.json()["error"].lower())

    def test_unknown_route_404_shape(self):
        res = self.client.get("/api/does-not-exist")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {"error": "not found"})

    def test_unauthenticated_when_dev_mode_off_is_401(self):
        # dev モードを切ると未認証は 401（認証ガードが効いていることの確認）。
        os.environ["AUTH_DEV_MODE"] = "false"
        try:
            res = self.client.get("/api/dashboard")
            self.assertEqual(res.status_code, 401)
        finally:
            os.environ["AUTH_DEV_MODE"] = "true"


if __name__ == "__main__":
    unittest.main()
