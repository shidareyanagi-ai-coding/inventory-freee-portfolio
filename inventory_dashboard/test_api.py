"""FastAPI 移行（EVOLUTION_PLAN.md A-1）の HTTP ルーティング層テスト。

test_app.py が業務ロジック関数を直接呼ぶのに対し、こちらは TestClient 経由で
「ルーティング・ステータスコード・エラー整形（{"error": ...}）」が旧 stdlib 実装と
同じ契約を保てているかを確認する。
"""

import os
import tempfile
import unittest

from fastapi.testclient import TestClient

import app


class InventoryApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_db_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp.name, "test_inventory.db")
        # TestClient を with（__enter__）で起動すると lifespan が走り init_db() される。
        self.client_cm = TestClient(app.app)
        self.client = self.client_cm.__enter__()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        app.DB_PATH = self.original_db_path
        self.tmp.cleanup()

    def test_index_serves_html_page(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers["content-type"])
        self.assertIn("在庫管理ダッシュボード", res.text)

    def test_dashboard_returns_expected_shape(self):
        res = self.client.get("/api/dashboard")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        for key in ("products", "total_stock_value", "product_count", "recent_movements"):
            self.assertIn(key, data)
        self.assertEqual(len(data["products"]), 3)

    def test_products_returns_seeded_list(self):
        res = self.client.get("/api/products")
        self.assertEqual(res.status_code, 200)
        skus = {p["sku"] for p in res.json()}
        self.assertIn("SKU-USB-C-001", skus)

    def test_forecast_simulation_accepts_horizon_query(self):
        res = self.client.get("/api/forecast-simulation", params={"horizon_days": 60})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["horizon_days"], 60)

    def test_create_purchase_returns_201_and_increases_stock(self):
        products = self.client.get("/api/products").json()
        product = products[0]
        before = product["stock_quantity"]

        res = self.client.post(
            "/api/purchases",
            json={
                "product_id": product["id"],
                "partner_name": "テスト仕入先",
                "invoice_no": "INV-API-001",
                "transaction_date": "2026-06-01",
                "received_date": "2026-06-02",
                "quantity": 4,
                "unit_price": 1000,
            },
        )
        self.assertEqual(res.status_code, 201)
        self.assertTrue(res.json()["ok"])

        after = next(p for p in self.client.get("/api/products").json() if p["id"] == product["id"])
        self.assertEqual(after["stock_quantity"], before + 4)

    def test_oversell_returns_400_with_error_message(self):
        product = self.client.get("/api/products").json()[0]
        res = self.client.post(
            "/api/sales",
            json={
                "product_id": product["id"],
                "partner_name": "テスト得意先",
                "invoice_no": "ORD-API-OVER",
                "quantity": product["stock_quantity"] + 1,
                "unit_price": 1500,
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("在庫不足", res.json()["error"])

    def test_freee_preview_after_purchase(self):
        product = self.client.get("/api/products").json()[0]
        created = self.client.post(
            "/api/purchases",
            json={
                "product_id": product["id"],
                "partner_name": "テスト仕入先",
                "invoice_no": "INV-API-PREVIEW",
                "quantity": 1,
                "unit_price": 1000,
            },
        ).json()

        res = self.client.get(
            "/api/freee-preview",
            params={"source_type": "purchase", "source_id": created["purchase_id"]},
        )
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertEqual(payload["type"], "expense")
        self.assertEqual(payload["invoice_no"], "INV-API-PREVIEW")

    def test_product_ledger_route(self):
        product = self.client.get("/api/products").json()[0]
        res = self.client.get(f"/api/products/{product['id']}/ledger")
        self.assertEqual(res.status_code, 200)
        self.assertIn("ledger", res.json())

    def test_unknown_route_returns_404_error_shape(self):
        res = self.client.get("/api/does-not-exist")
        self.assertEqual(res.status_code, 404)
        self.assertEqual(res.json(), {"error": "not found"})


if __name__ == "__main__":
    unittest.main()
