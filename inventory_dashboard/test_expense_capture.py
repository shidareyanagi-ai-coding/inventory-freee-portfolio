"""A-5 検証: 経費キャプチャ（AI証憑入力）。

EVOLUTION_PLAN.md「検証方法」6 を自動化する:
  - 画像 → AI解析 → フォーム反映で止まり、**自動登録されない**（登録は人）。
  - 低信頼度の項目がフォームで分かるように返る。
  - vouchers に元画像・AI抽出・人修正後が残る。
  併せて A-3 の原則も確認する: テナント分離(IDOR 404)・RBAC（viewer は更新系 403）。

実 Claude は不要: ANTHROPIC_API_KEY 未設定なら ai_capture が決定的スタブで下書きを返す。
route 系は AUTH_DEV_MODE=true で動かし、X-Dev-User-Id ヘッダで別ユーザ＝別組織を表す。
"""

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import ai_capture
import app


class _VoucherRouteBase(unittest.TestCase):
    """SQLite 一時DB + 証憑画像の一時保存先 + TestClient を立てる共通土台。"""

    def setUp(self):
        self._saved_env = {
            k: os.environ.get(k)
            for k in ("DATABASE_URL", "AUTH_DEV_MODE", "APP_ENV", "ANTHROPIC_API_KEY",
                      "CLERK_PUBLISHABLE_KEY", "CLERK_JWKS_URL", "CLERK_ISSUER")
        }
        for key in ("DATABASE_URL", "ANTHROPIC_API_KEY", "CLERK_PUBLISHABLE_KEY", "CLERK_JWKS_URL", "CLERK_ISSUER"):
            os.environ.pop(key, None)
        os.environ["APP_ENV"] = "development"
        os.environ["AUTH_DEV_MODE"] = "true"

        self.tmp = tempfile.TemporaryDirectory()
        self._orig_db_path = app.DB_PATH
        self._orig_voucher_dir = app.VOUCHER_DIR
        app.DB_PATH = os.path.join(self.tmp.name, "test_voucher.db")
        app.VOUCHER_DIR = Path(self.tmp.name) / "voucher_store"

        self.client_cm = TestClient(app.app)
        self.client = self.client_cm.__enter__()

    def tearDown(self):
        self.client_cm.__exit__(None, None, None)
        app.DB_PATH = self._orig_db_path
        app.VOUCHER_DIR = self._orig_voucher_dir
        self.tmp.cleanup()
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _headers(self, user_id):
        return {"X-Dev-User-Id": user_id}

    def _capture(self, user_id, content=b"FAKE-RECEIPT-IMAGE", name="receipt.png", mime="image/png"):
        return self.client.post(
            "/api/expense-capture",
            headers=self._headers(user_id),
            files={"file": (name, content, mime)},
        )


class CaptureDraftTest(_VoucherRouteBase):
    def test_capture_returns_draft_with_candidates_and_low_confidence(self):
        res = self._capture("userA")
        self.assertEqual(res.status_code, 201)
        body = res.json()
        # 下書きの全項目が返る。
        self.assertEqual(set(body["draft"].keys()), set(ai_capture.FIELDS))
        # 候補（勘定科目・税区分）が返る（フォームのドロップダウン用）。
        self.assertIn("消耗品費", body["account_item_candidates"])
        self.assertIn("課税仕入 10%", body["tax_category_candidates"])
        # 低信頼度の項目がフォームで分かるように返る（スタブは tax_category/memo を低信頼度にする）。
        self.assertTrue(body["low_confidence_fields"])
        self.assertIn("memo", body["low_confidence_fields"])
        # スタブ経路であることを明示（鍵があれば "anthropic" になる）。
        self.assertEqual(body["source"], "stub")

    def test_capture_without_image_is_400(self):
        res = self.client.post(
            "/api/expense-capture", headers=self._headers("userA"),
            files={"file": ("empty.png", b"", "image/png")},
        )
        self.assertEqual(res.status_code, 400)


class NoAutoRegisterTest(_VoucherRouteBase):
    """鉄則: AIは下書きまで。capture では**登録されない**。"""

    def test_capture_does_not_auto_register(self):
        voucher_id = self._capture("userA").json()["voucher_id"]
        detail = self.client.get(f"/api/vouchers/{voucher_id}", headers=self._headers("userA")).json()
        self.assertFalse(detail["registered"])         # 未登録
        self.assertIsNone(detail["user_corrected"])     # 人の修正後はまだ無い
        self.assertIsNone(detail["deal_id"])            # 会計伝票も作られていない
        self.assertTrue(detail["ai_extracted"])         # AI抽出だけは残っている

    def test_human_register_persists_corrected_and_audits(self):
        voucher_id = self._capture("userA").json()["voucher_id"]
        corrected = {
            "issue_date": "2026-06-10", "partner_name": "東京サプライ", "amount": 3300,
            "tax_category": "課税仕入 10%", "account_item": "消耗品費", "memo": "コピー用紙",
        }
        reg = self.client.post(
            f"/api/vouchers/{voucher_id}/register", headers=self._headers("userA"), json=corrected,
        )
        self.assertEqual(reg.status_code, 201)
        self.assertTrue(reg.json()["registered"])
        detail = self.client.get(f"/api/vouchers/{voucher_id}", headers=self._headers("userA")).json()
        self.assertTrue(detail["registered"])
        self.assertEqual(detail["user_corrected"]["partner_name"], "東京サプライ")
        self.assertEqual(detail["user_corrected"]["amount"], 3300)
        # 監査ログに人の登録が残る（admin 限定で閲覧）。
        actions = [r["action"] for r in self.client.get("/api/audit-logs", headers=self._headers("userA")).json()]
        self.assertIn("voucher.capture", actions)
        self.assertIn("voucher.register", actions)

    def test_register_rejects_missing_partner_or_nonpositive_amount(self):
        voucher_id = self._capture("userA").json()["voucher_id"]
        bad = self.client.post(
            f"/api/vouchers/{voucher_id}/register", headers=self._headers("userA"),
            json={"partner_name": "", "amount": 1000},
        )
        self.assertEqual(bad.status_code, 400)
        bad2 = self.client.post(
            f"/api/vouchers/{voucher_id}/register", headers=self._headers("userA"),
            json={"partner_name": "X", "amount": 0},
        )
        self.assertEqual(bad2.status_code, 400)


class VoucherStoresEvidenceTest(_VoucherRouteBase):
    """vouchers に元画像・AI抽出・人修正後が残る（後から見比べられる見せ場）。"""

    def test_image_ai_and_corrected_are_retrievable(self):
        content = b"ORIGINAL-RECEIPT-BYTES-123"
        voucher_id = self._capture("userA", content=content).json()["voucher_id"]
        # 元画像がそのまま取り出せる。
        img = self.client.get(f"/api/vouchers/{voucher_id}/image", headers=self._headers("userA"))
        self.assertEqual(img.status_code, 200)
        self.assertEqual(img.content, content)
        # AI抽出が残っている。
        detail = self.client.get(f"/api/vouchers/{voucher_id}", headers=self._headers("userA")).json()
        self.assertIn("fields", detail["ai_extracted"])
        # 人修正後を入れると残る。
        self.client.post(
            f"/api/vouchers/{voucher_id}/register", headers=self._headers("userA"),
            json={"partner_name": "関東OA商事", "amount": 500, "memo": "修正済み"},
        )
        detail2 = self.client.get(f"/api/vouchers/{voucher_id}", headers=self._headers("userA")).json()
        self.assertEqual(detail2["user_corrected"]["memo"], "修正済み")


class VoucherRbacTest(_VoucherRouteBase):
    """RBAC: viewer は capture/register 不可（403）、参照は可。"""

    def setUp(self):
        super().setUp()
        with app.get_conn() as conn:
            self.org_id = app.create_organization(conn, "経費RBAC組織")
            app.seed_organization(conn, self.org_id)
            app.set_membership(conn, self.org_id, "viewer-user", "viewer")
            app.set_membership(conn, self.org_id, "staff-user", "staff")

    def test_viewer_cannot_capture(self):
        self.assertEqual(self._capture("viewer-user").status_code, 403)

    def test_staff_can_capture(self):
        self.assertEqual(self._capture("staff-user").status_code, 201)

    def test_viewer_cannot_register_but_can_view(self):
        voucher_id = self._capture("staff-user").json()["voucher_id"]
        # viewer は参照できる。
        self.assertEqual(
            self.client.get("/api/vouchers", headers=self._headers("viewer-user")).status_code, 200
        )
        self.assertEqual(
            self.client.get(f"/api/vouchers/{voucher_id}", headers=self._headers("viewer-user")).status_code, 200
        )
        # viewer は登録できない（403）。
        reg = self.client.post(
            f"/api/vouchers/{voucher_id}/register", headers=self._headers("viewer-user"),
            json={"partner_name": "X", "amount": 100},
        )
        self.assertEqual(reg.status_code, 403)


class VoucherTenantIsolationTest(_VoucherRouteBase):
    """テナント分離(IDOR): 別組織の voucher_id は 404（詳細・画像・登録すべて）。"""

    def test_cross_tenant_voucher_is_404(self):
        voucher_a = self._capture("userA").json()["voucher_id"]
        # userB は別組織。userA の voucher を覗く/触ると 404。
        self.assertEqual(
            self.client.get(f"/api/vouchers/{voucher_a}", headers=self._headers("userB")).status_code, 404
        )
        self.assertEqual(
            self.client.get(f"/api/vouchers/{voucher_a}/image", headers=self._headers("userB")).status_code, 404
        )
        reg = self.client.post(
            f"/api/vouchers/{voucher_a}/register", headers=self._headers("userB"),
            json={"partner_name": "X", "amount": 100},
        )
        self.assertEqual(reg.status_code, 404)
        # 持ち主は 200。
        self.assertEqual(
            self.client.get(f"/api/vouchers/{voucher_a}", headers=self._headers("userA")).status_code, 200
        )

    def test_vouchers_list_is_tenant_scoped(self):
        self._capture("userA")
        self._capture("userB")
        list_a = self.client.get("/api/vouchers", headers=self._headers("userA")).json()
        list_b = self.client.get("/api/vouchers", headers=self._headers("userB")).json()
        self.assertEqual(len(list_a), 1)
        self.assertEqual(len(list_b), 1)
        self.assertNotEqual(list_a[0]["id"], list_b[0]["id"])


class AiCaptureStubTest(unittest.TestCase):
    """ai_capture の決定的スタブ（鍵なしでも下書きが返り、同じ画像は同じ結果）。"""

    def test_stub_is_deterministic_and_well_formed(self):
        first = ai_capture.analyze_voucher(b"same-bytes", "image/png")
        second = ai_capture.analyze_voucher(b"same-bytes", "image/png")
        self.assertEqual(first["fields"], second["fields"])
        self.assertEqual(first["source"], "stub")
        self.assertEqual(set(first["confidence"].keys()), set(ai_capture.FIELDS))
        # overall は最小信頼度。
        self.assertAlmostEqual(first["overall_confidence"], min(first["confidence"].values()))

    def test_empty_image_raises(self):
        with self.assertRaises(ValueError):
            ai_capture.analyze_voucher(b"", "image/png")


if __name__ == "__main__":
    unittest.main()
