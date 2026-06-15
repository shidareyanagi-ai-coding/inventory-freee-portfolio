"""需要予測レベル2（A-4）のテスト。

検証プラン 2/3/5（テナント分離・予測保存・MAE/MAPEバックテスト）と回帰を SQLite で確認する。
重い ML 依存（statsmodels/lightgbm）は importorskip で保護し、未導入環境でも baseline で緑になる。
高コストな run_forecast は setUpClass で一度だけ実行して使い回す。
"""

import os
import tempfile
import unittest

import app
import db
from forecasting import backtest, data, models, service


class ForecastingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # このテストは必ずローカル SQLite を使う（本番 DATABASE_URL を無視）。
        cls._original_database_url = os.environ.pop("DATABASE_URL", None)
        cls.tmp = tempfile.TemporaryDirectory()
        cls._original_db_path = app.DB_PATH
        app.DB_PATH = os.path.join(cls.tmp.name, "forecast.db")
        app.init_db()
        with app.get_conn() as conn:
            cls.org_id = app.create_organization(conn, "予測テスト組織")
            app.seed_organization(conn, cls.org_id)
            cls.other_org_id = app.create_organization(conn, "別テナント")
            app.seed_organization(conn, cls.other_org_id)
        with app.get_conn() as conn:
            cls.summary = service.run_forecast(conn, cls.org_id, horizon_days=14, test_days=14)

    @classmethod
    def tearDownClass(cls):
        app.DB_PATH = cls._original_db_path
        cls.tmp.cleanup()
        if cls._original_database_url is not None:
            os.environ["DATABASE_URL"] = cls._original_database_url

    def _first_product_id(self, conn, organization_id):
        return conn.execute(
            "SELECT id FROM products WHERE organization_id = ? ORDER BY id LIMIT 1",
            (organization_id,),
        ).fetchone()["id"]

    # --- 合成データ -------------------------------------------------------
    def test_demo_history_is_daily_with_external_factors(self):
        with app.get_conn() as conn:
            sale_days = conn.execute(
                """
                SELECT COUNT(DISTINCT transaction_date) AS days
                FROM sales WHERE organization_id = ? AND invoice_no LIKE 'DEMO-HIST-S-%'
                """,
                (self.org_id,),
            ).fetchone()["days"]
            factors = conn.execute(
                "SELECT COUNT(*) AS c FROM external_factors WHERE organization_id = ?",
                (self.org_id,),
            ).fetchone()["c"]
        # 月次2点ではなく「日次・多数日」になっていること（1年超の日数）。
        self.assertGreater(sale_days, 365)
        self.assertGreater(factors, 0)

    # --- 予測の書き込み ---------------------------------------------------
    def test_run_forecast_writes_all_tables_scoped_to_org(self):
        with app.get_conn() as conn:
            forecasts = conn.execute(
                "SELECT COUNT(*) AS c FROM forecasts WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            evaluations = conn.execute(
                "SELECT COUNT(*) AS c FROM model_evaluations WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            # 別テナントには予測が書かれていない（org 絞り込み）。
            other_forecasts = conn.execute(
                "SELECT COUNT(*) AS c FROM forecasts WHERE organization_id = ?", (self.other_org_id,)
            ).fetchone()["c"]
        self.assertGreater(forecasts, 0)
        self.assertGreater(evaluations, 0)
        self.assertEqual(other_forecasts, 0)
        self.assertEqual(self.summary["products_forecasted"], 3)
        self.assertIsNotNone(self.summary["best_model"])
        self.assertIn("baseline", self.summary["models"])

    def test_forecast_rerun_replaces_not_appends(self):
        with app.get_conn() as conn:
            before = conn.execute(
                "SELECT COUNT(*) AS c FROM forecasts WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
            service.run_forecast(conn, self.org_id, horizon_days=14, test_days=14)
            after = conn.execute(
                "SELECT COUNT(*) AS c FROM forecasts WHERE organization_id = ?", (self.org_id,)
            ).fetchone()["c"]
        self.assertEqual(before, after)

    def test_forecast_run_is_audited(self):
        with app.get_conn() as conn:
            audit = conn.execute(
                "SELECT COUNT(*) AS c FROM audit_logs WHERE organization_id = ? AND action = 'forecast.run'",
                (self.org_id,),
            ).fetchone()["c"]
        self.assertGreater(audit, 0)

    # --- バックテスト（MAE/MAPE）-----------------------------------------
    def test_backtest_returns_finite_mae(self):
        with app.get_conn() as conn:
            product_id = self._first_product_id(conn, self.org_id)
            series = data.load_demand_series(conn, self.org_id, product_id)
            factors = data.load_factor_pivot(conn, self.org_id, product_id)
        result = backtest.backtest_model(models.BaselineModel(), series, factors, test_days=14)
        self.assertIsNotNone(result)
        self.assertGreaterEqual(result["mae"], 0.0)
        self.assertEqual(result["n"], 14)

    # --- モデルレジストリ -------------------------------------------------
    def test_baseline_model_always_available(self):
        names = models.available_model_names()
        self.assertIn("baseline", names)

    def test_lightgbm_model_produces_forecast(self):
        if not models.LightGBMModel.available():
            self.skipTest("lightgbm 未導入")
        with app.get_conn() as conn:
            product_id = self._first_product_id(conn, self.org_id)
            series = data.load_demand_series(conn, self.org_id, product_id)
            factors = data.load_factor_pivot(conn, self.org_id, product_id)
        forecast = models.LightGBMModel().forecast(series, factors, horizon=14)
        self.assertEqual(len(forecast), 14)
        self.assertTrue((forecast["lower"] <= forecast["predicted"] + 1e-9).all())
        self.assertTrue((forecast["predicted"] <= forecast["upper"] + 1e-9).all())

    def test_sarima_model_produces_forecast(self):
        if not models.SarimaModel.available():
            self.skipTest("statsmodels 未導入")
        with app.get_conn() as conn:
            product_id = self._first_product_id(conn, self.org_id)
            series = data.load_demand_series(conn, self.org_id, product_id)
            factors = data.load_factor_pivot(conn, self.org_id, product_id)
        forecast = models.SarimaModel().forecast(series, factors, horizon=14)
        self.assertEqual(len(forecast), 14)
        self.assertTrue((forecast["predicted"] >= 0).all())

    # --- テナント分離（IDOR）---------------------------------------------
    def test_forecast_series_blocks_other_tenant_product(self):
        with app.get_conn() as conn:
            owner_product_id = self._first_product_id(conn, self.org_id)
            # 別テナントから他組織の product_id を渡すと NotFoundError（404 相当）。
            with self.assertRaises(app.NotFoundError):
                app.forecast_series(conn, self.other_org_id, owner_product_id)

    def test_forecast_series_returns_actual_and_forecast(self):
        with app.get_conn() as conn:
            owner_product_id = self._first_product_id(conn, self.org_id)
            series = app.forecast_series(conn, self.org_id, owner_product_id)
        self.assertEqual(series["product"]["id"], owner_product_id)
        self.assertGreater(len(series["actual"]), 0)
        self.assertGreater(len(series["forecast"]), 0)
        self.assertIn(series["model_name"], {"baseline", "sarima", "lightgbm"})

    # --- 回帰: 既存の月末シミュレーションが日次化後も動く ----------------
    def test_legacy_forecast_simulation_still_returns_rows(self):
        with app.get_conn() as conn:
            result = app.forecast_simulation(conn, self.org_id, 30)
        self.assertEqual(len(result["rows"]), 3)


class ForecastApiTest(unittest.TestCase):
    """API 層（認証・テナント絞り込み・IDOR 404）を TestClient で確認する。"""

    OWNER = "fc-owner"
    INTRUDER = "fc-intruder"

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient

        cls._original_database_url = os.environ.pop("DATABASE_URL", None)
        cls._saved_env = {k: os.environ.get(k) for k in ("AUTH_DEV_MODE", "APP_ENV")}
        os.environ["AUTH_DEV_MODE"] = "true"
        os.environ["APP_ENV"] = "development"
        cls.tmp = tempfile.TemporaryDirectory()
        cls._original_db_path = app.DB_PATH
        app.DB_PATH = os.path.join(cls.tmp.name, "forecast_api.db")
        cls.client_cm = TestClient(app.app)  # lifespan で init_db()
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)
        app.DB_PATH = cls._original_db_path
        cls.tmp.cleanup()
        for key, value in cls._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if cls._original_database_url is not None:
            os.environ["DATABASE_URL"] = cls._original_database_url

    def _owner_product_id(self):
        products = self.client.get("/api/products", headers={"X-Dev-User-Id": self.OWNER}).json()
        return products[0]["id"]

    def test_series_requires_auth(self):
        os.environ["AUTH_DEV_MODE"] = "false"
        try:
            res = self.client.get("/api/forecast/series?product_id=1")
            self.assertEqual(res.status_code, 401)
        finally:
            os.environ["AUTH_DEV_MODE"] = "true"

    def test_series_idor_returns_404_for_other_tenant(self):
        owner_product_id = self._owner_product_id()
        # 侵入者（別 dev ユーザー=別組織）が所有者の product_id を要求 → 404。
        res = self.client.get(
            f"/api/forecast/series?product_id={owner_product_id}",
            headers={"X-Dev-User-Id": self.INTRUDER},
        )
        self.assertEqual(res.status_code, 404)

    def test_run_forecast_then_series_and_evaluations(self):
        headers = {"X-Dev-User-Id": self.OWNER}
        run = self.client.post("/api/forecast/run?horizon_days=14", headers=headers)
        self.assertEqual(run.status_code, 200)
        self.assertIsNotNone(run.json()["best_model"])

        product_id = self._owner_product_id()
        series = self.client.get(f"/api/forecast/series?product_id={product_id}", headers=headers).json()
        self.assertGreater(len(series["forecast"]), 0)

        evaluations = self.client.get("/api/forecast/evaluations", headers=headers).json()
        self.assertGreater(len(evaluations), 0)

        candidates = self.client.get("/api/forecast/order-candidates", headers=headers)
        self.assertEqual(candidates.status_code, 200)

    def test_viewer_cannot_run_forecast(self):
        # viewer に降格したユーザーは予測バッチ（更新系）を実行できない（403）。
        viewer = "fc-viewer"
        self.client.get("/api/products", headers={"X-Dev-User-Id": viewer})  # 先に組織を作る
        with app.get_conn() as conn:
            membership = app.get_membership_by_user(conn, viewer)
            app.set_membership(conn, membership["organization_id"], viewer, "viewer")
        res = self.client.post("/api/forecast/run", headers={"X-Dev-User-Id": viewer})
        self.assertEqual(res.status_code, 403)


if __name__ == "__main__":
    unittest.main()
