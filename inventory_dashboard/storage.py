"""証憑画像の保存先を抽象化する（A-6 デプロイ）。

env の STORAGE_* が揃っていれば S3 互換オブジェクトストレージ（Cloudflare R2 など）に、
無ければローカルフォルダに保存する。呼び出し側（app.py）は「key（相対パス）」だけを扱い、
実際の置き場所はこのモジュールが決める。db.py が DATABASE_URL で SQLite/Postgres を
切り替えるのと同じ「env で切替・薄い手書きアダプタ」方針（説明しやすさ優先）。

なぜ必要か（A-6）: Render などのサーバはファイルが再起動で消える（ephemeral）ため、
本番では画像を外部のオブジェクトストレージに置く。ローカル開発・テストは env 未設定なので
従来どおりローカルフォルダを使う＝開発体験は変わらない。

秘密情報（STORAGE_SECRET_ACCESS_KEY 等）は環境変数のみ。コード/DB/ログには残さない。
"""

from __future__ import annotations

import os
from pathlib import Path


def _env(name: str) -> str:
    """環境変数を読む（前後空白を除去）。`.env` テンプレの行末コメントが値に混入した
    場合に備え '#' 始まりは未設定扱い（auth._env / ai_capture._api_key と同じ防御）。"""
    value = os.environ.get(name, "").strip()
    if value.startswith("#"):
        return ""
    return value


def _config() -> dict[str, str]:
    return {
        "endpoint": _env("STORAGE_ENDPOINT"),
        "region": _env("STORAGE_REGION") or "auto",  # R2 は "auto" でよい
        "bucket": _env("STORAGE_BUCKET"),
        "access_key": _env("STORAGE_ACCESS_KEY_ID"),
        "secret_key": _env("STORAGE_SECRET_ACCESS_KEY"),
    }


def object_storage_enabled() -> bool:
    """S3 互換ストレージ（R2 等）へ保存する設定が揃っているか。
    1 つでも欠ければ False＝ローカル保存にフォールバックする。"""
    c = _config()
    return all((c["endpoint"], c["bucket"], c["access_key"], c["secret_key"]))


def backend_name() -> str:
    """現在の保存先（起動ログやデバッグ用の表示名）。"""
    return "object-storage(S3/R2)" if object_storage_enabled() else "local-folder"


def _client():
    """boto3 の S3 クライアントを R2 等のエンドポイントに向けて作る（遅延 import）。
    boto3 未導入なら明示エラー（anthropic と同じく、使うときだけ依存を要求する）。"""
    try:
        import boto3
    except ModuleNotFoundError as exc:  # pragma: no cover - 依存未導入時のみ
        raise RuntimeError(
            "boto3 が未導入です（requirements.txt の boto3 行を有効化し pip install してください）"
        ) from exc
    c = _config()
    return boto3.client(
        "s3",
        endpoint_url=c["endpoint"],
        region_name=c["region"],
        aws_access_key_id=c["access_key"],
        aws_secret_access_key=c["secret_key"],
    )


# --- 公開 API（呼び出し側は key=相対パスだけを扱う） ------------------------

def save_bytes(local_dir: Path, key: str, data: bytes) -> None:
    """key（相対パス）にバイト列を保存する。"""
    if object_storage_enabled():
        _client().put_object(Bucket=_config()["bucket"], Key=key, Body=data)
        return
    path = Path(local_dir) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def read_bytes(local_dir: Path, key: str) -> bytes | None:
    """key のバイト列を返す。存在しなければ None（404 は呼び出し側で判断）。"""
    if object_storage_enabled():
        from botocore.exceptions import ClientError

        try:
            obj = _client().get_object(Bucket=_config()["bucket"], Key=key)
        except ClientError as exc:  # 鍵/エンドポイント誤りは握りつぶさず表に出す
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in ("NoSuchKey", "NoSuchBucket", "404", "NotFound"):
                return None
            raise
        return obj["Body"].read()
    path = Path(local_dir) / key
    if not path.exists():
        return None
    return path.read_bytes()


def delete(local_dir: Path, key: str) -> None:
    """key を削除する（存在しなくても・失敗しても例外を投げない＝ベストエフォート）。
    画像が消せなくても DB 行の削除は進めたいため。"""
    if object_storage_enabled():
        try:
            _client().delete_object(Bucket=_config()["bucket"], Key=key)
        except Exception:
            pass
        return
    path = Path(local_dir) / key
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass
