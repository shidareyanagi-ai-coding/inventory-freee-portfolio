"""A-5 検証: 仕入・売上請求書のAI取り込み（在庫ダッシュボード）。

経費キャプチャの再設計（EVOLUTION_PLAN.md）:
  - 在庫ダッシュボードは「仕入・売上の請求書」だけを扱う（一般経費は疑似freee側）。
  - 請求書画像 → AI下書き → 仕入/売上フォームに反映で止まり、**自動登録されない**（登録は人）。
  - 人が仕入/売上を登録すると、その証憑が紐付く（取込済）。証憑は削除できる。低信頼度が分かる。
  併せて A-3 原則: テナント分離(IDOR 404)・RBAC（viewer は更新系 403）。

実 Claude 不要: ANTHROPIC_API_KEY を外して ai_capture の決定的スタブで動かす。
route 系は AUTH_DEV_MODE=true + X-Dev-User-Id で擬似ログイン（ユーザ＝組織）。
"""

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import ai_capture
import app


class _Base(unittest.TestCase):
    """SQLite 一時DB + 証憑画像の一時保存先 + TestClient（スタブ経路）。"""

    def setUp(self):
        # STORAGE_* も退避＆除去＝テストは常にローカル保存（.env に R2 があっても実 R2 に書かない）。
        _storage_keys = ("STORAGE_ENDPOINT", "STORAGE_REGION", "STORAGE_BUCKET",
                         "STORAGE_ACCESS_KEY_ID", "STORAGE_SECRET_ACCESS_KEY")
        self._saved = {
            k: os.environ.get(k)
            for k in ("DATABASE_URL", "AUTH_DEV_MODE", "APP_ENV", "ANTHROPIC_API_KEY",
                      "CLERK_PUBLISHABLE_KEY", "CLERK_JWKS_URL", "CLERK_ISSUER", *_storage_keys)
        }
        for k in ("DATABASE_URL", "ANTHROPIC_API_KEY", "CLERK_PUBLISHABLE_KEY", "CLERK_JWKS_URL",
                  "CLERK_ISSUER", *_storage_keys):
            os.environ.pop(k, None)  # 鍵を外す＝決定的スタブで解析（本物AIを呼ばない）＋ローカル保存固定
        os.environ["APP_ENV"] = "development"
        os.environ["AUTH_DEV_MODE"] = "true"

        self.tmp = tempfile.TemporaryDirectory()
        self._db, self._vd = app.DB_PATH, app.VOUCHER_DIR
        app.DB_PATH = os.path.join(self.tmp.name, "t.db")
        app.VOUCHER_DIR = Path(self.tmp.name) / "voucher_store"

        self.cm = TestClient(app.app)
        self.client = self.cm.__enter__()

    def tearDown(self):
        self.cm.__exit__(None, None, None)
        app.DB_PATH, app.VOUCHER_DIR = self._db, self._vd
        self.tmp.cleanup()
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _h(self, user_id):
        return {"X-Dev-User-Id": user_id}

    def _capture(self, user_id, kind="purchase", content=b"FAKE-INVOICE-IMAGE", name="invoice.png", mime="image/png"):
        return self.client.post(
            f"/api/invoice-capture?kind={kind}", headers=self._h(user_id), files={"file": (name, content, mime)}
        )


class CaptureTest(_Base):
    def test_capture_returns_draft_and_does_not_register(self):
        res = self._capture("userA")
        self.assertEqual(res.status_code, 201)
        body = res.json()
        self.assertEqual(set(body["draft"].keys()), set(ai_capture.INVOICE_FIELDS))
        self.assertEqual(body["source"], "stub")
        self.assertEqual(body["kind"], "purchase")
        self.assertTrue(body["low_confidence_fields"])  # 商品照合などは低信頼度
        # 自動登録されない: capture だけでは取込済にならない。
        detail = self.client.get(f"/api/vouchers/{body['voucher_id']}", headers=self._h("userA")).json()
        self.assertFalse(detail["registered"])
        self.assertIsNone(detail["linked_source_type"])

    def test_sale_kind_works(self):
        res = self._capture("userA", kind="sale")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.json()["kind"], "sale")

    def test_invalid_kind_is_400(self):
        res = self.client.post("/api/invoice-capture?kind=bogus", headers=self._h("userA"), files={"file": ("x.png", b"x", "image/png")})
        self.assertEqual(res.status_code, 400)

    def test_empty_image_is_400(self):
        res = self.client.post("/api/invoice-capture?kind=purchase", headers=self._h("userA"), files={"file": ("x.png", b"", "image/png")})
        self.assertEqual(res.status_code, 400)


class LinkTest(_Base):
    """人が仕入/売上を登録すると、取り込んだ証憑が紐付く（取込済）。"""

    def test_register_purchase_links_voucher(self):
        cap = self._capture("userA").json()
        product = self.client.get("/api/products", headers=self._h("userA")).json()[0]
        created = self.client.post(
            "/api/purchases", headers=self._h("userA"),
            json={"product_id": product["id"], "partner_name": "取込仕入先", "invoice_no": "INV-LINK-1",
                  "quantity": 2, "unit_price": 1000, "voucher_id": cap["voucher_id"]},
        )
        self.assertEqual(created.status_code, 201)
        purchase_id = created.json()["purchase_id"]
        detail = self.client.get(f"/api/vouchers/{cap['voucher_id']}", headers=self._h("userA")).json()
        self.assertTrue(detail["registered"])
        self.assertEqual(detail["linked_source_type"], "purchase")
        self.assertEqual(detail["linked_source_id"], purchase_id)

    def test_purchase_without_voucher_id_still_works(self):
        product = self.client.get("/api/products", headers=self._h("userA")).json()[0]
        created = self.client.post(
            "/api/purchases", headers=self._h("userA"),
            json={"product_id": product["id"], "partner_name": "通常仕入先", "invoice_no": "INV-NOLINK", "quantity": 1, "unit_price": 500},
        )
        self.assertEqual(created.status_code, 201)


class DeleteTest(_Base):
    """ご指摘の「削除できない」対応: 証憑（DB行＋元画像）を削除できる。"""

    def test_delete_removes_voucher_and_image(self):
        vid = self._capture("userA").json()["voucher_id"]
        self.assertEqual(self.client.get(f"/api/vouchers/{vid}/image", headers=self._h("userA")).status_code, 200)
        deleted = self.client.delete(f"/api/vouchers/{vid}", headers=self._h("userA"))
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(self.client.get(f"/api/vouchers/{vid}", headers=self._h("userA")).status_code, 404)
        self.assertEqual(self.client.get(f"/api/vouchers/{vid}/image", headers=self._h("userA")).status_code, 404)
        self.assertEqual(len(self.client.get("/api/vouchers", headers=self._h("userA")).json()), 0)


class VoucherRbacTest(_Base):
    """RBAC: viewer は capture/delete 不可（403）、参照は可。"""

    def setUp(self):
        super().setUp()
        with app.get_conn() as conn:
            self.org = app.create_organization(conn, "INV-RBAC組織")
            app.seed_organization(conn, self.org)
            app.set_membership(conn, self.org, "viewer-user", "viewer")
            app.set_membership(conn, self.org, "staff-user", "staff")

    def test_viewer_cannot_capture(self):
        self.assertEqual(self._capture("viewer-user").status_code, 403)

    def test_staff_can_capture(self):
        self.assertEqual(self._capture("staff-user").status_code, 201)

    def test_viewer_cannot_delete_but_can_view(self):
        vid = self._capture("staff-user").json()["voucher_id"]
        self.assertEqual(self.client.get("/api/vouchers", headers=self._h("viewer-user")).status_code, 200)
        self.assertEqual(self.client.get(f"/api/vouchers/{vid}", headers=self._h("viewer-user")).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/vouchers/{vid}", headers=self._h("viewer-user")).status_code, 403)


class VoucherTenantIsolationTest(_Base):
    """テナント分離(IDOR): 別組織の voucher_id は 404（詳細・画像・削除すべて）。"""

    def test_cross_tenant_is_404(self):
        vid = self._capture("userA").json()["voucher_id"]
        self.assertEqual(self.client.get(f"/api/vouchers/{vid}", headers=self._h("userB")).status_code, 404)
        self.assertEqual(self.client.get(f"/api/vouchers/{vid}/image", headers=self._h("userB")).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/vouchers/{vid}", headers=self._h("userB")).status_code, 404)
        # 持ち主は見える＝消されていない。
        self.assertEqual(self.client.get(f"/api/vouchers/{vid}", headers=self._h("userA")).status_code, 200)


class AiInvoiceStubTest(unittest.TestCase):
    """ai_capture の請求書スタブ（鍵なしでも下書きが返り、同じ画像は同じ結果）。"""

    def setUp(self):
        self._k = os.environ.get("ANTHROPIC_API_KEY")
        os.environ.pop("ANTHROPIC_API_KEY", None)

    def tearDown(self):
        if self._k is not None:
            os.environ["ANTHROPIC_API_KEY"] = self._k

    def test_stub_is_deterministic_and_matches_product(self):
        products = [{"id": 1, "sku": "SKU-A", "product_name": "商品A", "supplier_name": "東京サプライ", "purchase_unit_price": 120, "sales_unit_price": 200}]
        a = ai_capture.analyze_invoice(b"same", "image/png", kind="purchase", products=products)
        b = ai_capture.analyze_invoice(b"same", "image/png", kind="purchase", products=products)
        self.assertEqual(a["fields"], b["fields"])
        self.assertEqual(a["source"], "stub")
        self.assertEqual(a["fields"]["product_sku"], "SKU-A")
        self.assertEqual(set(a["confidence"].keys()), set(ai_capture.INVOICE_FIELDS))

    def test_empty_image_raises(self):
        with self.assertRaises(ValueError):
            ai_capture.analyze_invoice(b"", kind="purchase")

    def test_bad_kind_raises(self):
        with self.assertRaises(ValueError):
            ai_capture.analyze_invoice(b"x", kind="bogus")


if __name__ == "__main__":
    unittest.main()
