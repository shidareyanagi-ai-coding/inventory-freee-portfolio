"""証憑解析（EVOLUTION_PLAN.md A-5: 経費キャプチャ / AI証憑入力）。

役割は「請求書・レシート画像 → 経費伝票の“下書き”(構造化JSON)」だけ。
鉄則（EVOLUTION_PLAN.md）: **AI は解析してフォームに仮入力するまで。「登録」は人が押す。**
このモジュールは DB にも pseudo_freee にも書かない（副作用なし）。登録は app.py 側が人の操作で行う。

設計境界（セキュリティ方針）:
  - 画像対応AI（既定は Claude）の呼び出しは **サーバ側のみ**。`ANTHROPIC_API_KEY` は環境変数で、
    ブラウザには絶対に出さない（鍵は app.py からも render_index に渡さない）。
  - 画像1枚ごとに少額の従量課金になるため、既定モデルは安価な vision 対応モデルにする
    （`ANTHROPIC_MODEL` で上書き可）。

テスト容易性（forecasting の遅延import/自動skipと同じ方針）:
  - `anthropic` 未導入、または `ANTHROPIC_API_KEY` 未設定のときは、**決定的なスタブ**で下書きを返す。
    これによりネットワーク無し・APIキー無しでも UI/テストが一通り動く（低信頼度表示も再現できる）。
  - 実APIの検証は鍵を入れたときだけ走る（returned dict の "source" で経路を見分けられる）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import date, timedelta
from typing import Any

# AI が推定する下書き項目（EXPENSE_CAPTURE_FEATURE_SPEC.md「AIが推定する項目」）。
FIELDS = ("issue_date", "partner_name", "amount", "tax_category", "account_item", "memo")

# この値未満の項目は「低信頼度」としてフォーム上で目立たせる（人の確認を促す）。
CONFIDENCE_THRESHOLD = 0.7

# 勘定科目・税区分の初期候補（EXPENSE_CAPTURE_FEATURE_SPEC.md「初期候補」）。
# pseudo_freee 側マスタと論理的に同じ。新FastAPI 側で自己完結させ、フォーム候補に使う。
ACCOUNT_ITEMS = (
    "消耗品費",
    "旅費交通費",
    "通信費",
    "荷造運賃",
    "支払手数料",
    "広告宣伝費",
    "会議費",
    "接待交際費",
    "水道光熱費",
    "地代家賃",
    "新聞図書費",
    "修繕費",
    "雑費",
    "仕入高",
)

TAX_CATEGORIES = (
    "課税仕入 10%",
    "課税仕入 8%",
    "対象外",
    "非課税",
    "不課税",
)


def account_item_candidates() -> list[str]:
    return list(ACCOUNT_ITEMS)


def tax_category_candidates() -> list[str]:
    return list(TAX_CATEGORIES)


def _api_key() -> str:
    """サーバ側のみ。`.env` の行末コメント混入を防ぐため '#' 始まりは未設定扱い（auth._env と同じ防御）。"""
    value = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if value.startswith("#"):
        return ""
    return value


def _model() -> str:
    value = os.environ.get("ANTHROPIC_MODEL", "").strip()
    # 既定は安価な vision + structured-outputs 対応モデル（証憑1枚あたりの課金を抑える）。
    return value or "claude-haiku-4-5"


def anthropic_ready() -> bool:
    """実 Claude vision を呼べる状態か（鍵あり かつ ライブラリ導入済み）。"""
    if not _api_key():
        return False
    try:
        import anthropic  # noqa: F401
    except Exception:
        return False
    return True


def low_confidence_fields(confidence: dict[str, float]) -> list[str]:
    """しきい値未満の項目名。フォームで目立たせる対象（単一の判定ロジック）。"""
    return [name for name in FIELDS if float(confidence.get(name, 0.0)) < CONFIDENCE_THRESHOLD]


def _finalize(fields: dict[str, Any], confidence: dict[str, float], source: str, model: str = "") -> dict[str, Any]:
    """下書きを共通形に整える。overall は最小信頼度（一番不安な項目に引っ張る）。"""
    conf = {name: round(float(confidence.get(name, 0.0)), 3) for name in FIELDS}
    overall = round(min(conf.values()), 3) if conf else 0.0
    draft = {name: fields.get(name, "") for name in FIELDS}
    # amount は数値に寄せる（後段の保存/表示が楽になる。失敗時は 0）。
    try:
        draft["amount"] = float(draft["amount"] or 0)
    except (TypeError, ValueError):
        draft["amount"] = 0.0
    return {
        "fields": draft,
        "confidence": conf,
        "overall_confidence": overall,
        "low_confidence_fields": low_confidence_fields(conf),
        "source": source,
        "model": model,
    }


# ---------------------------------------------------------------------------
# 決定的スタブ（鍵/ライブラリが無い開発・テスト用）
# ---------------------------------------------------------------------------
# 画像バイト列のハッシュから一貫した下書きを作る。実画像を読まないので中身は擬似的だが、
# 「フォームに仮入力 → 低信頼度の項目が目立つ → 人が直す」体験とテストには十分。
_STUB_PARTNERS = (
    "日本橋文具",
    "東京サプライ",
    "関東OA商事",
    "ヤマト運輸",
    "Amazonビジネス",
)


def _stub_analyze(image_bytes: bytes, mime_type: str) -> dict[str, Any]:
    digest = hashlib.sha256(image_bytes or b"empty").digest()
    n = int.from_bytes(digest[:8], "big")

    partner = _STUB_PARTNERS[n % len(_STUB_PARTNERS)]
    account = ACCOUNT_ITEMS[(n >> 3) % len(ACCOUNT_ITEMS)]
    # 税区分は勘定科目に応じて 10%/8% を割り当て（標準税区分の反映を擬似的に再現）。
    tax = "課税仕入 8%" if account in {"会議費", "接待交際費", "新聞図書費"} else "課税仕入 10%"
    amount = 1000 + (n % 9000)
    amount -= amount % 10  # 10円単位に丸める
    issue = (date.today() - timedelta(days=(n >> 7) % 14)).isoformat()

    fields = {
        "issue_date": issue,
        "partner_name": partner,
        "amount": amount,
        "tax_category": tax,
        "account_item": account,
        "memo": f"{partner} {account}",
    }
    # tax_category / memo は「読み取りにくい項目」として常に低信頼度にし、低信頼度表示を確実に再現する。
    confidence = {
        "issue_date": 0.93,
        "partner_name": 0.88,
        "amount": 0.86 if n % 2 == 0 else 0.58,
        "tax_category": 0.62,
        "account_item": 0.74,
        "memo": 0.55,
    }
    return _finalize(fields, confidence, source="stub")


# ---------------------------------------------------------------------------
# 実 Claude vision（鍵があるときだけ）
# ---------------------------------------------------------------------------
_SYSTEM = (
    "あなたは日本の個人事業の経理を補助するAIです。レシートや請求書の画像から、経費伝票の"
    "“下書き”を作ります。会計データを登録するのではなく、各項目を推定し、項目ごとに読み取りの"
    "信頼度(0.0〜1.0)を付けてください。読み取れない項目は空にし、低い信頼度を付けます。"
    "勘定科目は次から最も近いものを選ぶ: " + " / ".join(ACCOUNT_ITEMS) + "。"
    "税区分は次から選ぶ: " + " / ".join(TAX_CATEGORIES) + "。"
    "金額は税込の支払総額を数値(円)で。発生日は YYYY-MM-DD。"
)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "issue_date": {"type": "string", "description": "発生日 YYYY-MM-DD。読めなければ空文字"},
        "partner_name": {"type": "string", "description": "支払先名"},
        "amount": {"type": "number", "description": "税込支払総額（円）"},
        "tax_category": {"type": "string", "enum": [*TAX_CATEGORIES, ""]},
        "account_item": {"type": "string", "enum": [*ACCOUNT_ITEMS, ""]},
        "memo": {"type": "string", "description": "摘要（用途の短い説明）"},
        "confidence": {
            "type": "object",
            "properties": {name: {"type": "number"} for name in FIELDS},
            "required": list(FIELDS),
            "additionalProperties": False,
        },
    },
    "required": [*FIELDS, "confidence"],
    "additionalProperties": False,
}


def _analyze_with_anthropic(image_bytes: bytes, mime_type: str) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key())
    model = _model()
    image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    media_type = mime_type if mime_type in {"image/png", "image/jpeg", "image/gif", "image/webp"} else "image/jpeg"

    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                        {"type": "text", "text": "この画像から経費伝票の下書きを作ってください。"},
                    ],
                }
            ],
            # 構造化出力でスキーマに沿った JSON を強制する（先頭テキストブロックが妥当なJSON）。
            output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
        )
    except Exception as exc:  # APIError/接続失敗など。鍵があるのに失敗したら明示エラーにする。
        raise RuntimeError(f"AI解析に失敗しました: {exc}") from exc

    text = next((block.text for block in response.content if getattr(block, "type", "") == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AI応答のJSON解析に失敗しました") from exc

    confidence = data.get("confidence") or {}
    fields = {name: data.get(name, "") for name in FIELDS}
    return _finalize(fields, confidence, source="anthropic", model=model)


def analyze_voucher(image_bytes: bytes, mime_type: str = "") -> dict[str, Any]:
    """証憑画像から経費伝票の下書き(構造化JSON)を返す。副作用なし（登録しない）。

    鍵+ライブラリがあれば実 Claude vision、無ければ決定的スタブ。戻り値の "source" で見分けられる。
    用途: 一般経費（疑似freee側）。仕入・売上の請求書は analyze_invoice を使う。
    """
    if not image_bytes:
        raise ValueError("画像がありません。")
    if anthropic_ready():
        return _analyze_with_anthropic(image_bytes, mime_type)
    return _stub_analyze(image_bytes, mime_type)


# ===========================================================================
# 仕入・売上の請求書モード（在庫ダッシュボードの登録フォームに反映する）
# ===========================================================================
# 経費（analyze_voucher）と違い、商品・数量・単価など「在庫の仕入/売上フォーム」の項目を読む。
# 商品は既存の商品マスタ(products)から最も近いSKUを推測する（無ければ空＝人が選ぶ）。
INVOICE_FIELDS = ("partner_name", "invoice_no", "transaction_date", "product_sku", "quantity", "unit_price", "tax_rate")


def _finalize_invoice(fields: dict[str, Any], confidence: dict[str, float], source: str, kind: str, model: str = "") -> dict[str, Any]:
    conf = {name: round(float(confidence.get(name, 0.0)), 3) for name in INVOICE_FIELDS}
    overall = round(min(conf.values()), 3) if conf else 0.0
    draft = {name: fields.get(name, "") for name in INVOICE_FIELDS}
    # 数値項目は数値に寄せる（フォーム/保存が楽になる。失敗時は既定値）。
    try:
        draft["quantity"] = int(float(draft["quantity"] or 0))
    except (TypeError, ValueError):
        draft["quantity"] = 0
    try:
        draft["unit_price"] = float(draft["unit_price"] or 0)
    except (TypeError, ValueError):
        draft["unit_price"] = 0.0
    try:
        draft["tax_rate"] = float(draft["tax_rate"] or 10)
    except (TypeError, ValueError):
        draft["tax_rate"] = 10.0
    return {
        "fields": draft,
        "confidence": conf,
        "overall_confidence": overall,
        "low_confidence_fields": [n for n in INVOICE_FIELDS if conf.get(n, 0.0) < CONFIDENCE_THRESHOLD],
        "source": source,
        "model": model,
        "kind": kind,
    }


def _stub_invoice(image_bytes: bytes, mime_type: str, kind: str, products: list[dict[str, Any]]) -> dict[str, Any]:
    digest = hashlib.sha256(image_bytes or b"empty").digest()
    n = int.from_bytes(digest[:8], "big")

    product = products[n % len(products)] if products else {}
    sku = str(product.get("sku", "")) if product else ""
    supplier = str(product.get("supplier_name", "")) if product else ""
    partner = supplier or ("東京サプライ" if kind == "purchase" else "青山ECストア")
    qty = 1 + (n % 50)
    base = float(product.get("purchase_unit_price") or product.get("unit_price") or 0) if kind == "purchase" else float(product.get("sales_unit_price") or 0)
    unit_price = base if base > 0 else float(1000 + (n % 4000) - (n % 4000) % 10)
    issue = (date.today() - timedelta(days=(n >> 7) % 14)).isoformat()
    prefix = "INV" if kind == "purchase" else "ORD"

    fields = {
        "partner_name": partner,
        "invoice_no": f"{prefix}-{1000 + (n % 9000)}",
        "transaction_date": issue,
        "product_sku": sku,
        "quantity": qty,
        "unit_price": unit_price,
        "tax_rate": 10,
    }
    # 商品の対応付け(product_sku)は外れやすいので低信頼度にして「人の確認」を促す。
    confidence = {
        "partner_name": 0.86,
        "invoice_no": 0.9,
        "transaction_date": 0.92,
        "product_sku": 0.55 if sku else 0.2,
        "quantity": 0.8,
        "unit_price": 0.78,
        "tax_rate": 0.9,
    }
    return _finalize_invoice(fields, confidence, source="stub", kind=kind)


def _analyze_invoice_with_anthropic(image_bytes: bytes, mime_type: str, kind: str, products: list[dict[str, Any]]) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic(api_key=_api_key())
    model = _model()
    image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    media_type = mime_type if mime_type in {"image/png", "image/jpeg", "image/gif", "image/webp"} else "image/jpeg"

    partner_label = "仕入先（請求元）" if kind == "purchase" else "得意先（請求先）"
    catalog = "\n".join(f"  - {p.get('sku','')}: {p.get('product_name','')}" for p in products) or "  （登録商品なし）"
    skus = [str(p.get("sku", "")) for p in products if p.get("sku")]
    system = (
        f"あなたは日本の在庫管理担当を補助するAIです。{'仕入' if kind == 'purchase' else '売上'}の請求書/納品書の画像から、"
        "在庫システムの取引フォームに入れる下書きを作ります。登録はしません。各項目を推定し、項目ごとに信頼度(0.0〜1.0)を付けてください。"
        f"{partner_label}の名前、請求書番号、取引日(YYYY-MM-DD)、主要な明細1件の数量・税抜単価(円)・税率(10または8)を読み取ります。"
        "商品は次の登録済みマスタから最も近いものの SKU を選んでください（無ければ空文字）:\n" + catalog
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "partner_name": {"type": "string"},
            "invoice_no": {"type": "string"},
            "transaction_date": {"type": "string", "description": "YYYY-MM-DD。読めなければ空文字"},
            "product_sku": {"type": "string", **({"enum": [*skus, ""]} if skus else {})},
            "quantity": {"type": "number"},
            "unit_price": {"type": "number", "description": "税抜の単価（円）"},
            "tax_rate": {"type": "number", "description": "10 または 8"},
            "confidence": {
                "type": "object",
                "properties": {name: {"type": "number"} for name in INVOICE_FIELDS},
                "required": list(INVOICE_FIELDS),
                "additionalProperties": False,
            },
        },
        "required": [*INVOICE_FIELDS, "confidence"],
        "additionalProperties": False,
    }
    try:
        response = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                        {"type": "text", "text": f"この{'仕入' if kind == 'purchase' else '売上'}請求書から取引フォームの下書きを作ってください。"},
                    ],
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": schema}},
        )
    except Exception as exc:
        raise RuntimeError(f"AI解析に失敗しました: {exc}") from exc

    text = next((block.text for block in response.content if getattr(block, "type", "") == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AI応答のJSON解析に失敗しました") from exc

    fields = {name: data.get(name, "") for name in INVOICE_FIELDS}
    return _finalize_invoice(fields, data.get("confidence") or {}, source="anthropic", kind=kind, model=model)


def analyze_invoice(
    image_bytes: bytes, mime_type: str = "", *, kind: str = "purchase", products: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """仕入/売上の請求書画像から取引フォームの下書き(構造化JSON)を返す。副作用なし（登録しない）。

    kind: "purchase"(仕入) / "sale"(売上)。products: 既存商品マスタ（SKU推測に使う）。
    """
    if not image_bytes:
        raise ValueError("画像がありません。")
    if kind not in {"purchase", "sale"}:
        raise ValueError("kind は purchase または sale です。")
    products = products or []
    if anthropic_ready():
        return _analyze_invoice_with_anthropic(image_bytes, mime_type, kind, products)
    return _stub_invoice(image_bytes, mime_type, kind, products)
