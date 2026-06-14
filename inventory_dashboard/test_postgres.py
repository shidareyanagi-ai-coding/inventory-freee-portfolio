"""Postgres バックエンドのスモークテスト（EVOLUTION_PLAN.md A-2: Neon 移行）。

DATABASE_URL が postgres を指すときだけ実行する（未設定ならスキップ）。
方言差を吸収した経路が実際に動くことを Postgres 上で確認する:
  - IDENTITY 採番 + INSERT ... RETURNING id（db.insert_returning_id）
  - EXTRACT による月抽出（db.month_expr / forecast_simulation）
  - ON CONFLICT DO NOTHING / DO UPDATE（取引先・freeeキュー）
  - psycopg.IntegrityError → {"error": ...} 400 への整形

ローカル検証例（throwaway Postgres）:
  docker run -d --name a2pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
  $env:DATABASE_URL = "postgresql://postgres:pw@127.0.0.1:55432/postgres"
  python -m pytest test_postgres.py -q
"""

import json
import os
import unittest
import urllib.error
from unittest.mock import patch

import app
import db

DATABASE_URL = os.environ.get("DATABASE_URL", "")
RUN_PG = DATABASE_URL.startswith("postgres")

# A-2 のドメインテーブル一式（FK を無視してまとめて落とすため CASCADE）。
_DROP_ALL = """
DROP TABLE IF EXISTS inventory_corrections CASCADE;
DROP TABLE IF EXISTS freee_sync_queue CASCADE;
DROP TABLE IF EXISTS inventory_movements CASCADE;
DROP TABLE IF EXISTS sales CASCADE;
DROP TABLE IF EXISTS purchases CASCADE;
DROP TABLE IF EXISTS business_partners CASCADE;
DROP TABLE IF EXISTS products CASCADE;
"""


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


@unittest.skipUnless(RUN_PG, "DATABASE_URL が postgres を指していないためスキップ")
class PostgresSmokeTest(unittest.TestCase):
    def setUp(self):
        # 毎回まっさらなスキーマから（throwaway DB を想定）。
        with db.get_conn() as conn:
            conn.executescript(_DROP_ALL)
        app.init_db()

    def test_backend_is_postgres(self):
        with app.get_conn() as conn:
            self.assertTrue(conn.postgres)

    def test_seed_reproduced_on_postgres(self):
        with app.get_conn() as conn:
            products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
            partners = app.list_business_partners(conn)
            demo_sales = conn.execute(
                "SELECT COUNT(*) AS count FROM sales WHERE invoice_no LIKE 'DEMO-HIST-S-%'"
            ).fetchone()["count"]
        self.assertEqual(len(products), 3)
        self.assertIn("東京サプライ", partners["suppliers"])
        self.assertIn("青山ECストア", partners["customers"])
        self.assertGreater(demo_sales, 0)

    def test_purchase_returns_id_and_increases_stock(self):
        with app.get_conn() as conn:
            product = conn.execute("SELECT * FROM products ORDER BY id LIMIT 1").fetchone()
            before = app.stock_by_product(conn)[product["id"]]
            result = app.create_purchase(conn, {
                "product_id": product["id"],
                "partner_name": "PGテスト仕入先",
                "invoice_no": "INV-PG-001",
                "quantity": 5,
                "unit_price": 1000,
            })
            after = app.stock_by_product(conn)[product["id"]]
            queue = conn.execute(
                "SELECT * FROM freee_sync_queue WHERE source_type = 'purchase' AND source_id = ?",
                (result["purchase_id"],),
            ).fetchone()
        self.assertIsInstance(result["purchase_id"], int)
        self.assertEqual(after, before + 5)
        self.assertEqual(queue["status"], "pending")

    def test_oversell_raises(self):
        with app.get_conn() as conn:
            product = conn.execute("SELECT * FROM products ORDER BY id LIMIT 1").fetchone()
            current = app.stock_by_product(conn)[product["id"]]
            with self.assertRaises(ValueError):
                app.create_sale(conn, {
                    "product_id": product["id"],
                    "partner_name": "PGテスト得意先",
                    "invoice_no": "ORD-PG-OVER",
                    "quantity": current + 1,
                    "unit_price": 1500,
                })

    def test_cancel_marks_queue_failed(self):
        with app.get_conn() as conn:
            product = conn.execute("SELECT * FROM products ORDER BY id LIMIT 1").fetchone()
            result = app.create_purchase(conn, {
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
            app.cancel_inventory_movement(conn, {"movement_id": movement["id"], "reason": "商品選択ミス"})
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
            product = conn.execute("SELECT * FROM products ORDER BY id LIMIT 1").fetchone()
            result = app.create_purchase(conn, {
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
                send_result = app.send_queue_to_pseudo_freee(conn, {"id": queue["id"]})
            updated = conn.execute("SELECT * FROM freee_sync_queue WHERE id = ?", (queue["id"],)).fetchone()
        self.assertEqual(send_result["external_accounting_id"], "pseudo-freee-202")
        self.assertEqual(updated["status"], "sent")

    def test_forecast_uses_extract_month(self):
        # forecast_simulation は monthly_seasonal_factor 経由で EXTRACT(MONTH ...) を実行する。
        with app.get_conn() as conn:
            result = app.forecast_simulation(conn, 30)
        self.assertEqual(result["horizon_days"], 30)
        self.assertEqual(len(result["rows"]), 3)
        self.assertIn("required_inventory", result["rows"][0])

    def test_product_ledger_newest_first(self):
        with app.get_conn() as conn:
            product = conn.execute("SELECT * FROM products ORDER BY id LIMIT 1").fetchone()
            ledger = app.product_ledger(conn, product["id"])["ledger"]
        dates = [row["movement_date"] for row in ledger]
        self.assertEqual(dates, sorted(dates, reverse=True))


@unittest.skipUnless(RUN_PG, "DATABASE_URL が postgres を指していないためスキップ")
class PostgresRouteTest(unittest.TestCase):
    """TestClient 経由でルーティング層が Postgres でも旧契約どおり動くか確認する。"""

    def setUp(self):
        from fastapi.testclient import TestClient

        with db.get_conn() as conn:
            conn.executescript(_DROP_ALL)
        self.client_cm = TestClient(app.app)  # lifespan で init_db()
        self.client = self.client_cm.__enter__()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)

    def test_dashboard_ok(self):
        res = self.client.get("/api/dashboard")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.json()["products"]), 3)

    def test_create_purchase_201(self):
        product = self.client.get("/api/products").json()[0]
        res = self.client.post("/api/purchases", json={
            "product_id": product["id"],
            "partner_name": "PGルート仕入先",
            "invoice_no": "INV-PG-ROUTE",
            "quantity": 3,
            "unit_price": 1000,
        })
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.json()["ok"])

    def test_oversell_400(self):
        product = self.client.get("/api/products").json()[0]
        res = self.client.post("/api/sales", json={
            "product_id": product["id"],
            "partner_name": "PGルート得意先",
            "invoice_no": "ORD-PG-ROUTE-OVER",
            "quantity": product["stock_quantity"] + 1,
            "unit_price": 1500,
        })
        self.assertEqual(res.status_code, 400)
        self.assertIn("在庫不足", res.json()["error"])

    def test_duplicate_sku_integrity_error_is_400(self):
        # UNIQUE(sku) 違反 → psycopg.IntegrityError → 例外ハンドラで 400 に整形。
        body = {"sku": "SKU-PG-DUP", "product_name": "重複SKU商品"}
        first = self.client.post("/api/products", json=body)
        self.assertEqual(first.status_code, 201)
        second = self.client.post("/api/products", json=body)
        self.assertEqual(second.status_code, 400)
        self.assertIn("integrity", second.json()["error"].lower())

    def test_unknown_route_404_shape(self):
        res = self.client.get("/api/does-not-exist")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {"error": "not found"})


if __name__ == "__main__":
    unittest.main()
