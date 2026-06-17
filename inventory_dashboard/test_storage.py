"""A-6: 証憑画像の保存先抽象化（storage.py）の単体テスト。

- env 未設定なら「ローカルフォルダ」保存で往復できる。
- STORAGE_* が揃うと「S3/R2 経路」に切り替わる（実 R2 不要・boto3 クライアントをモック）。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import storage

try:
    import boto3  # noqa: F401

    _BOTO3 = True
except Exception:
    _BOTO3 = False

_STORAGE_KEYS = (
    "STORAGE_ENDPOINT",
    "STORAGE_REGION",
    "STORAGE_BUCKET",
    "STORAGE_ACCESS_KEY_ID",
    "STORAGE_SECRET_ACCESS_KEY",
)


class StorageLocalTest(unittest.TestCase):
    """STORAGE_* 未設定＝ローカル保存。"""

    def setUp(self):
        self._saved = {k: os.environ.pop(k, None) for k in _STORAGE_KEYS}
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "voucher_store"

    def tearDown(self):
        self.tmp.cleanup()
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v

    def test_disabled_without_env(self):
        self.assertFalse(storage.object_storage_enabled())
        self.assertEqual(storage.backend_name(), "local-folder")

    def test_local_round_trip(self):
        storage.save_bytes(self.dir, "7/abc_receipt.png", b"hello")
        self.assertEqual(storage.read_bytes(self.dir, "7/abc_receipt.png"), b"hello")

    def test_missing_key_returns_none(self):
        self.assertIsNone(storage.read_bytes(self.dir, "nope/missing.png"))

    def test_delete_is_best_effort(self):
        storage.save_bytes(self.dir, "7/x.png", b"data")
        storage.delete(self.dir, "7/x.png")
        self.assertIsNone(storage.read_bytes(self.dir, "7/x.png"))
        storage.delete(self.dir, "7/x.png")  # 二度目（存在しない）でも例外を投げない

    def test_partial_env_stays_local(self):
        # 一部だけ設定では有効化しない（鍵欠落で実 R2 に書こうとしない安全側）。
        os.environ["STORAGE_BUCKET"] = "only-bucket"
        try:
            self.assertFalse(storage.object_storage_enabled())
        finally:
            os.environ.pop("STORAGE_BUCKET", None)


@unittest.skipUnless(_BOTO3, "boto3 未導入のため S3/R2 経路テストはスキップ")
class StorageObjectModeTest(unittest.TestCase):
    """STORAGE_* が揃うと S3/R2 経路に切り替わる（boto3 クライアントをモック）。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in _STORAGE_KEYS}
        os.environ.update(
            {
                "STORAGE_ENDPOINT": "https://acc.r2.cloudflarestorage.com",
                "STORAGE_REGION": "auto",
                "STORAGE_BUCKET": "inventory-vouchers",
                "STORAGE_ACCESS_KEY_ID": "AKIA_TEST",
                "STORAGE_SECRET_ACCESS_KEY": "secret_test",
            }
        )
        self.dir = Path("/unused")  # オブジェクト経路ではローカルパスを使わない

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_enabled_with_env(self):
        self.assertTrue(storage.object_storage_enabled())
        self.assertIn("S3/R2", storage.backend_name())

    @patch("storage._client")
    def test_save_routes_to_put_object(self, mock_client):
        client = MagicMock()
        mock_client.return_value = client
        storage.save_bytes(self.dir, "7/x.png", b"data")
        client.put_object.assert_called_once_with(
            Bucket="inventory-vouchers", Key="7/x.png", Body=b"data"
        )

    @patch("storage._client")
    def test_read_routes_to_get_object(self, mock_client):
        client = MagicMock()
        body = MagicMock()
        body.read.return_value = b"data"
        client.get_object.return_value = {"Body": body}
        mock_client.return_value = client
        self.assertEqual(storage.read_bytes(self.dir, "7/x.png"), b"data")
        client.get_object.assert_called_once_with(Bucket="inventory-vouchers", Key="7/x.png")

    @patch("storage._client")
    def test_delete_routes_to_delete_object(self, mock_client):
        client = MagicMock()
        mock_client.return_value = client
        storage.delete(self.dir, "7/x.png")
        client.delete_object.assert_called_once_with(Bucket="inventory-vouchers", Key="7/x.png")


if __name__ == "__main__":
    unittest.main()
