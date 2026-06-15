"""認証層（EVOLUTION_PLAN.md A-3: Clerk JWT 検証）。

役割は「Bearer トークン(Clerk JWT)を JWKS で署名検証し、claims を返す」ことだけ。
DB/組織解決・RBAC は app.py が担う（このモジュールは app を import しない＝循環回避）。

設計境界（EVOLUTION_PLAN.md「データアクセス境界」「セキュリティ方針」）:
  - 自前認証は作らない。署名検証は Clerk の公開鍵(JWKS)で行う。
  - 認可（organization_id 絞り込み・ロール判定）は FastAPI 側=app.py が単一の主体。
  - シークレット（CLERK_SECRET_KEY 等）はサーバ側・環境変数のみ。検証に必要なのは
    公開鍵(JWKS)だけなので、このモジュールはシークレットを読まない。

テスト容易性:
  - 実 Clerk が無くても検証経路を試せるよう、署名鍵の取得は _signing_key_for_token に
    集約してある（テストは自己署名 RSA 鍵でここを差し替える）。
  - AUTH_DEV_MODE=true のときは、トークン無しのローカル開発/テストを app 側が許可する。
"""

from __future__ import annotations

import os
from typing import Any


class AuthError(Exception):
    """認証失敗（401 相当）。app 側で HTTP 401 に整形する。"""


def auth_dev_mode() -> bool:
    """Clerk 未設定でもローカルで動かす開発モード。本番では必ず false。"""
    if app_env() == "production":
        return False
    return os.environ.get("AUTH_DEV_MODE", "").strip().lower() in {"1", "true", "yes", "on"}


def app_env() -> str:
    return os.environ.get("APP_ENV", "development").strip().lower()


def _env(name: str) -> str:
    """環境変数を読む（前後空白を除去）。

    防御: `.env` テンプレの「行末コメント」が値に混入した場合に備える。
    python-dotenv は「空の値 + ` # コメント`」の行で `#` 以降を値として読んでしまうため、
    '#' で始まる値（CLERK_* 等に正規の値が '#' で始まることはない）は未設定とみなす。
    """
    value = os.environ.get(name, "").strip()
    if value.startswith("#"):
        return ""
    return value


def clerk_issuer() -> str:
    return _env("CLERK_ISSUER").rstrip("/")


def clerk_jwks_url() -> str:
    url = _env("CLERK_JWKS_URL")
    if url:
        return url
    issuer = clerk_issuer()
    if issuer:
        return f"{issuer}/.well-known/jwks.json"
    return ""


def clerk_publishable_key() -> str:
    """フロントに渡してよい公開キー（ブラウザに出して問題ないもの）。"""
    return _env("CLERK_PUBLISHABLE_KEY")


def clerk_configured() -> bool:
    return bool(clerk_jwks_url())


# JWKS クライアントは鍵をキャッシュするので URL ごとに 1 つだけ作る。
_jwk_clients: dict[str, Any] = {}


def _jwk_client(jwks_url: str) -> Any:
    client = _jwk_clients.get(jwks_url)
    if client is None:
        from jwt import PyJWKClient

        client = PyJWKClient(jwks_url)
        _jwk_clients[jwks_url] = client
    return client


def _signing_key_for_token(token: str) -> Any:
    """token の kid に対応する公開鍵を JWKS から取得する。

    テストはこの関数を差し替えて自己署名鍵を返す（ネットワーク不要で検証経路を試す）。
    """
    jwks_url = clerk_jwks_url()
    if not jwks_url:
        raise AuthError("CLERK_JWKS_URL（または CLERK_ISSUER）が未設定です")
    try:
        return _jwk_client(jwks_url).get_signing_key_from_jwt(token).key
    except Exception as exc:  # PyJWKClientError / ネットワーク等
        raise AuthError(f"署名鍵の取得に失敗しました: {exc}") from exc


def verify_token(token: str) -> dict[str, Any]:
    """Clerk JWT を検証して claims(dict) を返す。失敗時は AuthError。

    検証内容: RS256 署名、exp(期限)、sub(必須)、issuer（設定時のみ）。
    Clerk のセッショントークンは既定で aud を持たないため aud 検証はしない。
    """
    try:
        import jwt
    except ModuleNotFoundError as exc:  # PyJWT 未導入
        raise AuthError("PyJWT が未導入です（requirements.txt の A-3 行を有効化してください）") from exc

    if not token:
        raise AuthError("トークンがありません")

    signing_key = _signing_key_for_token(token)
    issuer = clerk_issuer()
    try:
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            issuer=issuer or None,
            options={
                "require": ["exp", "sub"],
                "verify_aud": False,
                "verify_iss": bool(issuer),
            },
        )
    except Exception as exc:  # ExpiredSignatureError / InvalidTokenError ほか
        raise AuthError(f"トークン検証に失敗しました: {exc}") from exc

    if not claims.get("sub"):
        raise AuthError("sub クレームがありません")
    return claims


def bearer_token_from_header(authorization: str | None) -> str | None:
    """`Authorization: Bearer <token>` から token を取り出す。無ければ None。"""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None
