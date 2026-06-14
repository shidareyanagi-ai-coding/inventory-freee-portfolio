"""A-3 検証: 認証ガード・テナント分離(IDOR)・RBAC・JWT署名検証。

EVOLUTION_PLAN.md「検証方法」1〜3 を自動化する:
  1. 認証ガード: 未認証で各 API に到達不可（401）。
  2. テナント分離(IDOR): 別組織の id を直接渡しても 404。
  3. RBAC: viewer は更新系不可（403）、staff/admin は可。

route 系は AUTH_DEV_MODE=true で動かし、X-Dev-User-Id ヘッダで別ユーザ＝別組織を表す。
署名検証(verify_token)は自己署名 RSA 鍵で検証経路だけを単体テストする（実 Clerk 不要）。
"""

import os
import tempfile
import time
import unittest
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

import app
import auth


def _make_client():
    cm = TestClient(app.app)  # lifespan で init_db()（スキーマ作成）
    return cm


class _SqliteRouteBase(unittest.TestCase):
    """SQLite 一時DB + TestClient を立てる共通土台。"""

    auth_dev_mode = "true"

    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("DATABASE_URL", "AUTH_DEV_MODE", "APP_ENV", "CLERK_PUBLISHABLE_KEY", "CLERK_JWKS_URL", "CLERK_ISSUER")
        }
        os.environ.pop("DATABASE_URL", None)
        os.environ.pop("CLERK_PUBLISHABLE_KEY", None)
        os.environ.pop("CLERK_JWKS_URL", None)
        os.environ.pop("CLERK_ISSUER", None)
        os.environ["APP_ENV"] = "development"
        if self.auth_dev_mode is None:
            os.environ.pop("AUTH_DEV_MODE", None)
        else:
            os.environ["AUTH_DEV_MODE"] = self.auth_dev_mode

        self.tmp = tempfile.TemporaryDirectory()
        self._original_db_path = app.DB_PATH
        app.DB_PATH = os.path.join(self.tmp.name, "test_auth.db")

        self.client_cm = TestClient(app.app)
        self.client = self.client_cm.__enter__()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        app.DB_PATH = self._original_db_path
        self.tmp.cleanup()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _headers(self, user_id):
        return {"X-Dev-User-Id": user_id}


class AuthGuardTest(_SqliteRouteBase):
    """1. 認証ガード: dev モード OFF・トークン無し → 401。"""

    auth_dev_mode = "false"

    def test_unauthenticated_requests_are_rejected(self):
        for path in ["/api/dashboard", "/api/products", "/api/forecast-simulation", "/api/freee-sync-queue"]:
            res = self.client.get(path)
            self.assertEqual(res.status_code, 401, path)

    def test_unauthenticated_write_is_rejected(self):
        res = self.client.post("/api/purchases", json={"product_id": 1, "invoice_no": "X", "quantity": 1})
        self.assertEqual(res.status_code, 401)

    def test_index_is_public(self):
        res = self.client.get("/")
        self.assertEqual(res.status_code, 200)


class FirstLoginSeedTest(_SqliteRouteBase):
    def test_first_login_provisions_and_seeds_sandbox(self):
        res = self.client.get("/api/products", headers=self._headers("user_new"))
        self.assertEqual(res.status_code, 200)
        # 初回ログインで自組織にデモ seed（3商品）が入る。
        self.assertEqual(len(res.json()), 3)


class TenantIsolationTest(_SqliteRouteBase):
    """2. テナント分離(IDOR): 別組織の id を直接渡しても 404。"""

    def setUp(self):
        super().setUp()
        # userA / userB はそれぞれ初回ログインで別組織＋デモ seed。
        self.products_a = self.client.get("/api/products", headers=self._headers("userA")).json()
        self.products_b = self.client.get("/api/products", headers=self._headers("userB")).json()

    def test_each_tenant_sees_its_own_products_only(self):
        ids_a = {p["id"] for p in self.products_a}
        ids_b = {p["id"] for p in self.products_b}
        self.assertEqual(ids_a & ids_b, set())

    def test_cross_tenant_ledger_is_404(self):
        product_a_id = self.products_a[0]["id"]
        # userB が userA の product_id で元帳を要求 → 404。
        res = self.client.get(f"/api/products/{product_a_id}/ledger", headers=self._headers("userB"))
        self.assertEqual(res.status_code, 404)
        # 持ち主 userA は 200。
        ok = self.client.get(f"/api/products/{product_a_id}/ledger", headers=self._headers("userA"))
        self.assertEqual(ok.status_code, 200)

    def test_cross_tenant_freee_preview_is_404(self):
        product_a_id = self.products_a[0]["id"]
        created = self.client.post(
            "/api/purchases",
            headers=self._headers("userA"),
            json={"product_id": product_a_id, "partner_name": "A仕入先", "invoice_no": "INV-A-1", "quantity": 1, "unit_price": 100},
        )
        self.assertEqual(created.status_code, 201)
        purchase_id = created.json()["purchase_id"]
        # userB が userA の purchase を覗こうとする → 404。
        res = self.client.get(
            "/api/freee-preview",
            headers=self._headers("userB"),
            params={"source_type": "purchase", "source_id": purchase_id},
        )
        self.assertEqual(res.status_code, 404)

    def test_cross_tenant_cancel_is_404(self):
        product_a_id = self.products_a[0]["id"]
        self.client.post(
            "/api/purchases",
            headers=self._headers("userA"),
            json={"product_id": product_a_id, "partner_name": "A仕入先", "invoice_no": "INV-A-2", "quantity": 2, "unit_price": 100},
        )
        ledger = self.client.get(f"/api/products/{product_a_id}/ledger", headers=self._headers("userA")).json()
        movement_id = ledger["ledger"][0]["id"]
        # userB が userA の movement を取消そうとする → 404（在庫も動かない）。
        res = self.client.post(
            "/api/inventory-movements/cancel",
            headers=self._headers("userB"),
            json={"movement_id": movement_id, "reason": "不正取消"},
        )
        self.assertEqual(res.status_code, 404)


class RbacTest(_SqliteRouteBase):
    """3. RBAC: 同一組織内で viewer は更新系不可、staff/admin は可。"""

    def setUp(self):
        super().setUp()
        # 1組織を作り、その中に viewer / staff / admin を所属させる。
        with app.get_conn() as conn:
            self.org_id = app.create_organization(conn, "RBAC組織")
            app.seed_organization(conn, self.org_id)
            app.set_membership(conn, self.org_id, "viewer-user", "viewer")
            app.set_membership(conn, self.org_id, "staff-user", "staff")
            app.set_membership(conn, self.org_id, "admin-user", "admin")
        self.product_id = self.client.get("/api/products", headers=self._headers("viewer-user")).json()[0]["id"]

    def _purchase_body(self, invoice_no):
        return {"product_id": self.product_id, "partner_name": "RBAC仕入先", "invoice_no": invoice_no, "quantity": 1, "unit_price": 100}

    def test_viewer_can_read(self):
        res = self.client.get("/api/dashboard", headers=self._headers("viewer-user"))
        self.assertEqual(res.status_code, 200)

    def test_viewer_cannot_write(self):
        res = self.client.post("/api/purchases", headers=self._headers("viewer-user"), json=self._purchase_body("INV-V-1"))
        self.assertEqual(res.status_code, 403)

    def test_staff_can_write(self):
        res = self.client.post("/api/purchases", headers=self._headers("staff-user"), json=self._purchase_body("INV-S-1"))
        self.assertEqual(res.status_code, 201)

    def test_admin_can_write(self):
        res = self.client.post("/api/purchases", headers=self._headers("admin-user"), json=self._purchase_body("INV-AD-1"))
        self.assertEqual(res.status_code, 201)

    def test_audit_log_records_writes_and_is_admin_only(self):
        self.client.post("/api/purchases", headers=self._headers("staff-user"), json=self._purchase_body("INV-AUDIT-1"))
        # viewer は監査ログ閲覧不可（admin 限定）。
        forbidden = self.client.get("/api/audit-logs", headers=self._headers("viewer-user"))
        self.assertEqual(forbidden.status_code, 403)
        # admin は閲覧でき、直前の purchase.create が記録されている。
        logs = self.client.get("/api/audit-logs", headers=self._headers("admin-user"))
        self.assertEqual(logs.status_code, 200)
        actions = [row["action"] for row in logs.json()]
        self.assertIn("purchase.create", actions)


class VerifyTokenTest(unittest.TestCase):
    """Clerk 無しで JWT 署名検証(RS256)の経路を単体テストする。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("CLERK_ISSUER",)}
        os.environ.pop("CLERK_ISSUER", None)  # issuer 検証は無効化して署名/exp/sub に集中
        self.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.private_pem = self.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        self.public_key = self.private_key.public_key()

    def tearDown(self):
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _token(self, **overrides):
        payload = {"sub": "user_x", "exp": int(time.time()) + 3600}
        payload.update(overrides)
        return jwt.encode(payload, self.private_pem, algorithm="RS256", headers={"kid": "test-kid"})

    def test_valid_token_returns_claims(self):
        token = self._token()
        with patch.object(auth, "_signing_key_for_token", return_value=self.public_key):
            claims = auth.verify_token(token)
        self.assertEqual(claims["sub"], "user_x")

    def test_expired_token_raises(self):
        token = self._token(exp=int(time.time()) - 10)
        with patch.object(auth, "_signing_key_for_token", return_value=self.public_key):
            with self.assertRaises(auth.AuthError):
                auth.verify_token(token)

    def test_tampered_token_raises(self):
        token = self._token()
        tampered = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
        with patch.object(auth, "_signing_key_for_token", return_value=self.public_key):
            with self.assertRaises(auth.AuthError):
                auth.verify_token(tampered)

    def test_missing_sub_raises(self):
        # sub 無し: PyJWT の require=["sub"] で弾かれる。
        token = jwt.encode({"exp": int(time.time()) + 3600}, self.private_pem, algorithm="RS256", headers={"kid": "test-kid"})
        with patch.object(auth, "_signing_key_for_token", return_value=self.public_key):
            with self.assertRaises(auth.AuthError):
                auth.verify_token(token)


if __name__ == "__main__":
    unittest.main()
