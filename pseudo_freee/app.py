from __future__ import annotations

import base64
import hashlib
import html
import json
import os
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import ai_capture
import auth
import db
import storage

try:
    # .env があれば ANTHROPIC_API_KEY 等を読み込む（無ければ何もしない）。
    # python-dotenv 未導入でも疑似freee は stdlib で動く（AI はお試しモードになる）。
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "pseudo_freee.db"
# A-5 ステップ2: 証憑（レシート）の元画像はサーバ側のここに保存し、DBにはパスのみを持つ（.gitignore 済み）。
# 本番(A-6)ではオブジェクトストレージ(S3互換/R2)に差し替える前提（EVOLUTION_PLAN.md「画像保存」）。
VOUCHER_DIR = APP_DIR / "voucher_store"
# 本番(Render等)は環境変数 PORT で待ち受けポートが渡され、外部公開のため 0.0.0.0 にバインドする。
# ローカルは従来どおり 127.0.0.1:8010。PORT が来ているか（=クラウド上か）で自動で切り替える（A-6）。
# 在庫アプリ(inventory_dashboard/app.py)と同じ方針＝説明しやすさのため作りを揃える。
PORT = int(os.environ.get("PORT") or os.environ.get("PSEUDO_FREEE_PORT", "8010"))
HOST = os.environ.get("PSEUDO_FREEE_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
# A-6:「入口ページ＋アプリ選択」用。在庫アプリ(=入口ページの置き場所)の公開URL。
# 設定時、疑似freee の画面上部に「← アプリ入口へ」リンクを出す（未設定なら出さない）。
INVENTORY_APP_URL = os.environ.get("INVENTORY_APP_URL", "").strip().rstrip("/")


DEALS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pseudo_freee_deals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queue_id INTEGER,
    source_app TEXT NOT NULL DEFAULT 'inventory_dashboard',
    source_type TEXT NOT NULL CHECK (source_type IN ('purchase', 'sale', 'manual_expense')),
    source_id INTEGER,
    deal_type TEXT NOT NULL CHECK (deal_type IN ('expense', 'income')),
    issue_date TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL DEFAULT '',
    partner_name TEXT NOT NULL DEFAULT '',
    partner_master_id INTEGER,
    freee_partner_id TEXT NOT NULL DEFAULT '',
    invoice_no TEXT NOT NULL DEFAULT '',
    account_item_name TEXT NOT NULL DEFAULT '',
    tax_category TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    memo TEXT NOT NULL DEFAULT '',
    payment_method TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(queue_id, source_type, source_id)
);
"""

LINES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pseudo_freee_deal_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER NOT NULL REFERENCES pseudo_freee_deals(id) ON DELETE CASCADE,
    sku TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    quantity REAL NOT NULL DEFAULT 0,
    unit_price REAL NOT NULL DEFAULT 0,
    tax_rate REAL NOT NULL DEFAULT 0,
    tax_category TEXT NOT NULL DEFAULT '',
    amount REAL NOT NULL DEFAULT 0,
    account_item_name TEXT NOT NULL DEFAULT ''
);
"""

INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_pseudo_freee_deals_created_at
ON pseudo_freee_deals(created_at);

CREATE INDEX IF NOT EXISTS idx_pseudo_freee_deals_issue_date
ON pseudo_freee_deals(issue_date);
"""

MASTER_SQL = """
CREATE TABLE IF NOT EXISTS pseudo_freee_payees (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payee_name TEXT NOT NULL UNIQUE,
    search_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pseudo_freee_account_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_item_name TEXT NOT NULL UNIQUE,
    default_tax_category TEXT NOT NULL DEFAULT '課税仕入 10%',
    search_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS pseudo_freee_tax_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tax_category TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

# A-5 ステップ2: 証憑（レシート画像 + AI下書き + 人の確定内容）。
# AIは下書き(ai_extracted_json)まで。人が登録すると deal_id と user_corrected_json が入る（=取込済み）。
# 元画像は storage_path（サーバ側ファイル）に置き、DBにはパスのみ（EVOLUTION_PLAN.md「画像保存」）。
VOUCHERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pseudo_freee_vouchers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    deal_id INTEGER REFERENCES pseudo_freee_deals(id),
    file_name TEXT NOT NULL DEFAULT '',
    storage_path TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    ai_extracted_json TEXT NOT NULL DEFAULT '{}',
    user_corrected_json TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_pseudo_freee_vouchers_created_at
ON pseudo_freee_vouchers(created_at);
"""

SCHEMA_SQL = f"""
PRAGMA foreign_keys = ON;

{DEALS_TABLE_SQL}

{LINES_TABLE_SQL}

{INDEX_SQL}

{MASTER_SQL}

{VOUCHERS_TABLE_SQL}
"""

DEFAULT_PAYEES = [
    "日本橋文具",
    "東京サプライ",
    "関東OA商事",
    "ヤマト運輸",
    "佐川急便",
    "日本郵便",
    "Amazonビジネス",
    "Google",
    "Microsoft",
]

DEFAULT_ACCOUNT_ITEMS = [
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
]

DEFAULT_ACCOUNT_ITEM_TAX_CATEGORIES = {
    "対象外": {"支払手数料"},
}

DEFAULT_TAX_CATEGORIES = [
    "課税仕入 10%",
    "課税仕入 8%",
    "対象外",
    "非課税",
    "不課税",
]

DEFAULT_MASTER_SEARCH_KEYS = {
    "Amazonビジネス": "amazon",
    "Google": "google",
    "Microsoft": "microsoft",
    "ヤマト運輸": "yamato",
    "佐川急便": "sagawa",
    "新宿デザイン事務所": "shinjuku",
    "日本橋文具": "nihonbashi",
    "日本郵便": "yubin",
    "東京サプライ": "tokyo",
    "関東OA商事": "kanto",
    "青山ECストア": "aoyama",
    "仕入高": "shi",
    "会議費": "kai",
    "修繕費": "shu",
    "地代家賃": "chi",
    "売上高": "uri",
    "広告宣伝費": "kou",
    "接待交際費": "set",
    "支払手数料": "shiha",
    "新聞図書費": "shin",
    "旅費交通費": "ryo",
    "水道光熱費": "sui",
    "消耗品費": "sho",
    "研修費": "ken",
    "荷造運賃": "nizu",
    "通信費": "tsu",
    "雑費": "zatsu",
}


def db_connection() -> Any:
    """DB接続を返す（A-8: db.py が DATABASE_URL で SQLite/Postgres を自動切替）。
    DATABASE_URL が postgres:// なら Neon、無ければローカル SQLite（DB_PATH）。
    `with db_connection() as conn:` の使い方は従来どおり（db.get_conn が境界を管理）。"""
    return db.get_conn(DB_PATH)


def table_exists(conn: db.Connection, table_name: str) -> bool:
    return db.table_exists(conn, table_name)


def deal_schema_needs_migration(conn: db.Connection) -> bool:
    if not table_exists(conn, "pseudo_freee_deals"):
        return False
    table_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'pseudo_freee_deals'"
    ).fetchone()["sql"]
    columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(pseudo_freee_deals)").fetchall()}
    return (
        "manual_expense" not in table_sql
        or "memo" not in columns
        or bool(columns["queue_id"]["notnull"])
        or bool(columns["source_id"]["notnull"])
    )


def migrate_deals_schema(conn: db.Connection) -> None:
    if conn.postgres:
        return  # Postgres は新規DBで現行スキーマを作る＝SQLite時代の旧スキーマ移行は不要
    if not deal_schema_needs_migration(conn):
        return

    old_columns = {row["name"] for row in conn.execute("PRAGMA table_info(pseudo_freee_deals)").fetchall()}
    has_lines = table_exists(conn, "pseudo_freee_deal_lines")

    conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("ALTER TABLE pseudo_freee_deals RENAME TO pseudo_freee_deals_legacy")
    if has_lines:
        conn.execute("ALTER TABLE pseudo_freee_deal_lines RENAME TO pseudo_freee_deal_lines_legacy")
    conn.executescript(f"{DEALS_TABLE_SQL}\n{LINES_TABLE_SQL}\n{INDEX_SQL}")

    memo_select = "memo" if "memo" in old_columns else "''"
    conn.execute(
        f"""
        INSERT INTO pseudo_freee_deals (
            id, queue_id, source_app, source_type, source_id, deal_type,
            issue_date, due_date, partner_name, partner_master_id,
            freee_partner_id, invoice_no, account_item_name, tax_category,
            amount, memo, payload_json, created_at
        )
        SELECT
            id, queue_id, source_app, source_type, source_id, deal_type,
            issue_date, due_date, partner_name, partner_master_id,
            freee_partner_id, invoice_no, account_item_name, tax_category,
            amount, {memo_select}, payload_json, created_at
        FROM pseudo_freee_deals_legacy
        """
    )
    if has_lines:
        conn.execute(
            """
            INSERT INTO pseudo_freee_deal_lines (
                id, deal_id, sku, description, quantity, unit_price,
                tax_rate, tax_category, amount, account_item_name
            )
            SELECT
                id, deal_id, sku, description, quantity, unit_price,
                tax_rate, tax_category, amount, account_item_name
            FROM pseudo_freee_deal_lines_legacy
            """
        )
        conn.execute("DROP TABLE pseudo_freee_deal_lines_legacy")
    conn.execute("DROP TABLE pseudo_freee_deals_legacy")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")


def ensure_master_schema(conn: db.Connection) -> None:
    if conn.postgres:
        return  # Postgres は現行スキーマで全列が揃う＝SQLite向けの後付け列追加は不要
    if table_exists(conn, "pseudo_freee_payees"):
        payee_columns = {row["name"] for row in conn.execute("PRAGMA table_info(pseudo_freee_payees)").fetchall()}
        if "search_key" not in payee_columns:
            conn.execute("ALTER TABLE pseudo_freee_payees ADD COLUMN search_key TEXT NOT NULL DEFAULT ''")

    if table_exists(conn, "pseudo_freee_account_items"):
        account_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(pseudo_freee_account_items)").fetchall()
        }
        if "default_tax_category" not in account_columns:
            conn.execute(
                "ALTER TABLE pseudo_freee_account_items ADD COLUMN default_tax_category TEXT NOT NULL DEFAULT '課税仕入 10%'"
            )
        if "search_key" not in account_columns:
            conn.execute("ALTER TABLE pseudo_freee_account_items ADD COLUMN search_key TEXT NOT NULL DEFAULT ''")


def ensure_deals_columns(conn: db.Connection) -> None:
    """既存DBに後付けした列を補う（payment_method / content_hash）。データは保持する。"""
    if conn.postgres:
        return  # Postgres は現行スキーマで全列が揃う＝SQLite向けの後付け列追加は不要
    if table_exists(conn, "pseudo_freee_deals"):
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(pseudo_freee_deals)").fetchall()}
        if "payment_method" not in columns:
            conn.execute("ALTER TABLE pseudo_freee_deals ADD COLUMN payment_method TEXT NOT NULL DEFAULT ''")
    if table_exists(conn, "pseudo_freee_vouchers"):
        vcolumns = {row["name"] for row in conn.execute("PRAGMA table_info(pseudo_freee_vouchers)").fetchall()}
        if "content_hash" not in vcolumns:
            conn.execute("ALTER TABLE pseudo_freee_vouchers ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")


def backfill_voucher_hashes(conn: db.Connection) -> None:
    """content_hash が空の既存証憑に、保存済み画像からハッシュを計算して埋める。

    重複検知を「列を足す前に保存した証憑」にも効かせるための後付け処理（冪等）。
    """
    if not table_exists(conn, "pseudo_freee_vouchers"):
        return
    rows = conn.execute(
        "SELECT id, storage_path FROM pseudo_freee_vouchers WHERE content_hash = '' AND storage_path != ''"
    ).fetchall()
    for row in rows:
        try:
            data = storage.read_bytes(VOUCHER_DIR, row["storage_path"])  # A-8: R2/ローカルを自動判定
            if data is not None:
                digest = hashlib.sha256(data).hexdigest()
                conn.execute(
                    "UPDATE pseudo_freee_vouchers SET content_hash = ? WHERE id = ?", (digest, row["id"])
                )
        except OSError:
            pass  # 画像が読めない証憑はスキップ（次回起動時に再試行される）


def default_tax_for_account_item(account_item_name: str) -> str:
    for tax_category, account_items in DEFAULT_ACCOUNT_ITEM_TAX_CATEGORIES.items():
        if account_item_name in account_items:
            return tax_category
    return "課税仕入 10%"


def default_search_key_for_master(name: str) -> str:
    return DEFAULT_MASTER_SEARCH_KEYS.get(name, "")


def seed_master_data(conn: db.Connection) -> None:
    conn.executemany(
        "INSERT INTO pseudo_freee_payees (payee_name) VALUES (?) ON CONFLICT DO NOTHING",
        [(name,) for name in DEFAULT_PAYEES],
    )
    conn.executemany(
        """
        INSERT INTO pseudo_freee_account_items (account_item_name, default_tax_category)
        VALUES (?, ?)
        ON CONFLICT DO NOTHING
        """,
        [(name, default_tax_for_account_item(name)) for name in DEFAULT_ACCOUNT_ITEMS],
    )
    conn.executemany(
        """
        UPDATE pseudo_freee_account_items
        SET default_tax_category = ?
        WHERE account_item_name = ? AND default_tax_category = ''
        """,
        [(default_tax_for_account_item(name), name) for name in DEFAULT_ACCOUNT_ITEMS],
    )
    conn.executemany(
        """
        UPDATE pseudo_freee_payees
        SET search_key = ?
        WHERE payee_name = ? AND search_key = ''
        """,
        [(default_search_key_for_master(name), name) for name in DEFAULT_MASTER_SEARCH_KEYS],
    )
    conn.executemany(
        """
        UPDATE pseudo_freee_account_items
        SET search_key = ?
        WHERE account_item_name = ? AND search_key = ''
        """,
        [(default_search_key_for_master(name), name) for name in DEFAULT_MASTER_SEARCH_KEYS],
    )
    conn.executemany(
        "INSERT INTO pseudo_freee_tax_categories (tax_category) VALUES (?) ON CONFLICT DO NOTHING",
        [(name,) for name in DEFAULT_TAX_CATEGORIES],
    )
    conn.execute(
        """
        INSERT INTO pseudo_freee_payees (payee_name)
        SELECT DISTINCT partner_name
        FROM pseudo_freee_deals
        WHERE partner_name != ''
        ON CONFLICT DO NOTHING
        """
    )
    conn.execute(
        """
        INSERT INTO pseudo_freee_account_items (account_item_name)
        SELECT DISTINCT account_item_name
        FROM pseudo_freee_deals
        WHERE account_item_name != ''
        ON CONFLICT DO NOTHING
        """
    )
    conn.execute(
        """
        INSERT INTO pseudo_freee_tax_categories (tax_category)
        SELECT DISTINCT tax_category
        FROM pseudo_freee_deals
        WHERE tax_category != ''
        ON CONFLICT DO NOTHING
        """
    )
    conn.executemany(
        """
        UPDATE pseudo_freee_payees
        SET search_key = ?
        WHERE payee_name = ? AND search_key = ''
        """,
        [(default_search_key_for_master(name), name) for name in DEFAULT_MASTER_SEARCH_KEYS],
    )
    conn.executemany(
        """
        UPDATE pseudo_freee_account_items
        SET search_key = ?
        WHERE account_item_name = ? AND search_key = ''
        """,
        [(default_search_key_for_master(name), name) for name in DEFAULT_MASTER_SEARCH_KEYS],
    )


def init_db() -> None:
    with db_connection() as conn:
        migrate_deals_schema(conn)    # SQLite の旧スキーマのみ移行（Postgres では何もしない）
        db.create_schema(conn)        # 方言別スキーマ（CREATE TABLE IF NOT EXISTS で冪等）
        ensure_master_schema(conn)    # SQLite の旧DBに不足列を追加（Postgres では何もしない）
        ensure_deals_columns(conn)    # 同上
        backfill_voucher_hashes(conn) # 画像からハッシュ補完（両方言）
        seed_master_data(conn)        # マスタ投入（両方言・ON CONFLICT DO NOTHING）


def row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0") or "0")
    if length <= 0:
        raise ValueError("JSON body is required")
    raw_body = handler.rfile.read(length).decode("utf-8")
    try:
        data = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise ValueError("invalid JSON body") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON body must be an object")
    return data


def to_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def to_float(value: Any, default: float = 0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required")
    return text


def remember_expense_masters(
    conn: db.Connection,
    payee_name: str,
    account_item_name: str,
    tax_category: str,
) -> None:
    if payee_name:
        conn.execute("INSERT INTO pseudo_freee_payees (payee_name) VALUES (?) ON CONFLICT DO NOTHING", (payee_name,))
    if account_item_name:
        conn.execute(
            """
            INSERT INTO pseudo_freee_account_items (account_item_name, default_tax_category)
            VALUES (?, ?)
            ON CONFLICT DO NOTHING
            """,
            (account_item_name, tax_category or "課税仕入 10%"),
        )
    if tax_category:
        conn.execute(
            "INSERT INTO pseudo_freee_tax_categories (tax_category) VALUES (?) ON CONFLICT DO NOTHING",
            (tax_category,),
        )


def list_expense_masters(conn: db.Connection) -> dict[str, Any]:
    payee_rows = conn.execute(
        """
        SELECT payee_name, search_key
        FROM pseudo_freee_payees
        ORDER BY payee_name
        """
    ).fetchall()
    payees = [row["payee_name"] for row in payee_rows]
    account_item_rows = conn.execute(
        """
        SELECT account_item_name, default_tax_category, search_key
        FROM pseudo_freee_account_items
        ORDER BY account_item_name
        """
    ).fetchall()
    account_items = [row["account_item_name"] for row in account_item_rows]
    tax_categories = [
        row["tax_category"]
        for row in conn.execute(
            "SELECT tax_category FROM pseudo_freee_tax_categories ORDER BY tax_category"
        ).fetchall()
    ]
    return {
        "payees": payees,
        "payee_settings": [row_to_dict(row) for row in payee_rows],
        "account_items": account_items,
        "account_item_settings": [row_to_dict(row) for row in account_item_rows],
        "tax_categories": tax_categories,
    }


def create_expense_master(conn: db.Connection, data: dict[str, Any]) -> dict[str, Any]:
    master_type = str(data.get("master_type", "") or "").strip()
    name = required_text(data.get("name"), "name")
    search_key = str(data.get("search_key", "") or "").strip()
    if master_type == "payee":
        conn.execute(
            """
            INSERT INTO pseudo_freee_payees (payee_name, search_key)
            VALUES (?, ?)
            ON CONFLICT(payee_name) DO UPDATE SET
                search_key = excluded.search_key
            """,
            (name, search_key),
        )
    elif master_type == "account_item":
        default_tax_category = str(data.get("default_tax_category", "課税仕入 10%") or "課税仕入 10%")
        conn.execute(
            """
            INSERT INTO pseudo_freee_account_items (account_item_name, default_tax_category, search_key)
            VALUES (?, ?, ?)
            ON CONFLICT(account_item_name) DO UPDATE SET
                default_tax_category = excluded.default_tax_category,
                search_key = excluded.search_key
            """,
            (name, default_tax_category, search_key),
        )
        conn.execute(
            "INSERT INTO pseudo_freee_tax_categories (tax_category) VALUES (?) ON CONFLICT DO NOTHING",
            (default_tax_category,),
        )
    elif master_type == "tax_category":
        conn.execute("INSERT INTO pseudo_freee_tax_categories (tax_category) VALUES (?) ON CONFLICT DO NOTHING", (name,))
    else:
        raise ValueError("master_type must be payee, account_item, or tax_category")
    return {"ok": True, "master_type": master_type, "name": name, "search_key": search_key}


def normalize_deal_request(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")

    queue_id = to_int(data.get("queue_id"), "queue_id")
    source_id = to_int(data.get("source_id"), "source_id")
    source_type = str(data.get("source_type", "")).strip()
    if source_type not in {"purchase", "sale"}:
        raise ValueError("source_type must be purchase or sale")

    deal_type = str(payload.get("type", "")).strip()
    if deal_type not in {"expense", "income"}:
        raise ValueError("payload.type must be expense or income")

    details = payload.get("details")
    if not isinstance(details, list) or not details:
        raise ValueError("payload.details must be a non-empty array")

    normalized_lines: list[dict[str, Any]] = []
    for detail in details:
        if not isinstance(detail, dict):
            raise ValueError("payload.details entries must be objects")
        normalized_lines.append(
            {
                "sku": str(detail.get("sku", "") or ""),
                "description": str(detail.get("description", "") or ""),
                "quantity": to_float(detail.get("quantity")),
                "unit_price": to_float(detail.get("unit_price")),
                "tax_rate": to_float(detail.get("tax_rate")),
                "tax_category": str(detail.get("tax_category", "") or ""),
                "amount": to_float(detail.get("amount")),
                "account_item_name": str(detail.get("account_item_name", "") or ""),
            }
        )

    first_line = normalized_lines[0]
    partner_master_id = payload.get("partner_master_id")
    partner_master_id = None if partner_master_id in (None, "") else to_int(partner_master_id, "payload.partner_master_id")

    return {
        "queue_id": queue_id,
        "source_app": str(data.get("source_app", "inventory_dashboard") or "inventory_dashboard"),
        "source_type": source_type,
        "source_id": source_id,
        "deal_type": deal_type,
        "issue_date": str(payload.get("issue_date", "") or ""),
        "due_date": str(payload.get("due_date", "") or ""),
        "partner_name": str(payload.get("partner_name", "") or ""),
        "partner_master_id": partner_master_id,
        "freee_partner_id": str(payload.get("freee_partner_id", "") or ""),
        "invoice_no": str(payload.get("invoice_no", "") or ""),
        "account_item_name": first_line["account_item_name"],
        "tax_category": first_line["tax_category"],
        "amount": sum(line["amount"] for line in normalized_lines),
        "memo": str(payload.get("memo", "") or ""),
        "payload_json": json.dumps(data, ensure_ascii=False, indent=2),
        "lines": normalized_lines,
    }


def create_deal(conn: db.Connection, data: dict[str, Any]) -> tuple[int, bool]:
    deal = normalize_deal_request(data)
    existing = conn.execute(
        """
        SELECT id
        FROM pseudo_freee_deals
        WHERE queue_id = ? AND source_type = ? AND source_id = ?
        """,
        (deal["queue_id"], deal["source_type"], deal["source_id"]),
    ).fetchone()
    if existing:
        return int(existing["id"]), False

    deal_id = db.insert_returning_id(
        conn,
        """
        INSERT INTO pseudo_freee_deals (
            queue_id, source_app, source_type, source_id, deal_type,
            issue_date, due_date, partner_name, partner_master_id,
            freee_partner_id, invoice_no, account_item_name, tax_category,
            amount, memo, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            deal["queue_id"],
            deal["source_app"],
            deal["source_type"],
            deal["source_id"],
            deal["deal_type"],
            deal["issue_date"],
            deal["due_date"],
            deal["partner_name"],
            deal["partner_master_id"],
            deal["freee_partner_id"],
            deal["invoice_no"],
            deal["account_item_name"],
            deal["tax_category"],
            deal["amount"],
            deal["memo"],
            deal["payload_json"],
        ),
    )
    conn.executemany(
        """
        INSERT INTO pseudo_freee_deal_lines (
            deal_id, sku, description, quantity, unit_price, tax_rate,
            tax_category, amount, account_item_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                deal_id,
                line["sku"],
                line["description"],
                line["quantity"],
                line["unit_price"],
                line["tax_rate"],
                line["tax_category"],
                line["amount"],
                line["account_item_name"],
            )
            for line in deal["lines"]
        ],
    )
    return deal_id, True


def create_manual_expense(conn: db.Connection, data: dict[str, Any]) -> dict[str, Any]:
    issue_date = required_text(data.get("issue_date"), "issue_date")
    partner_name = required_text(data.get("partner_name"), "partner_name")
    account_item_name = required_text(data.get("account_item_name"), "account_item_name")
    amount = to_float(data.get("amount"))
    if amount <= 0:
        raise ValueError("amount must be greater than 0")

    payment_method = str(data.get("payment_method", "現金") or "現金")
    if payment_method not in {"現金", "普通預金", "未払金"}:
        raise ValueError("payment_method must be 現金, 普通預金, or 未払金")
    due_date = str(data.get("due_date", "") or "")
    # 現金/普通預金は即時決済＝支払予定日を持たない（未払金のときだけ予定日を残す）。
    if payment_method != "未払金":
        due_date = ""
    tax_category = str(data.get("tax_category", "課税仕入 10%") or "課税仕入 10%")
    description = str(data.get("description", "") or account_item_name)
    memo = str(data.get("memo", "") or "")
    tax_rate = to_float(data.get("tax_rate"), 10)
    payload = {
        "source_app": "manual",
        "source_type": "manual_expense",
        "payload": {
            "api_target": "pseudo_freee_manual_expense",
            "issue_date": issue_date,
            "due_date": due_date,
            "type": "expense",
            "payment_method": payment_method,
            "partner_name": partner_name,
            "invoice_no": "",
            "memo": memo,
            "details": [
                {
                    "sku": "",
                    "description": description,
                    "quantity": 1,
                    "unit_price": amount,
                    "tax_rate": tax_rate,
                    "tax_category": tax_category,
                    "amount": amount,
                    "account_item_name": account_item_name,
                }
            ],
        },
    }

    deal_id = db.insert_returning_id(
        conn,
        """
        INSERT INTO pseudo_freee_deals (
            queue_id, source_app, source_type, source_id, deal_type,
            issue_date, due_date, partner_name, partner_master_id,
            freee_partner_id, invoice_no, account_item_name, tax_category,
            amount, memo, payment_method, payload_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            None,
            "manual",
            "manual_expense",
            None,
            "expense",
            issue_date,
            due_date,
            partner_name,
            None,
            "",
            "",
            account_item_name,
            tax_category,
            amount,
            memo,
            payment_method,
            json.dumps(payload, ensure_ascii=False, indent=2),
        ),
    )
    conn.execute(
        """
        INSERT INTO pseudo_freee_deal_lines (
            deal_id, sku, description, quantity, unit_price, tax_rate,
            tax_category, amount, account_item_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (deal_id, "", description, 1, amount, tax_rate, tax_category, amount, account_item_name),
    )
    remember_expense_masters(conn, partner_name, account_item_name, tax_category)
    # 請求書/レシートから取り込んで登録した場合、その証憑(voucher_id)を経費伝票に紐付ける。
    _maybe_link_voucher(conn, data, deal_id)
    return {"ok": True, "pseudo_freee_deal_id": deal_id}


def delete_deal(conn: db.Connection, deal_id: int) -> bool:
    """取引を削除する（明細も削除）。手入力経費のみ。存在しない/手入力以外なら False。

    在庫連携の取引（仕入/売上）は在庫ダッシュボードが唯一の正のため、ここでは削除しない
    （pseudo_freee 側で消すと在庫数の計算と食い違うため）。
    紐付く証憑があれば「下書き」に戻す（deal_id と確定内容をクリア）。証憑画像自体は残す。
    """
    existing = conn.execute(
        "SELECT id, source_type FROM pseudo_freee_deals WHERE id = ?", (deal_id,)
    ).fetchone()
    if not existing or existing["source_type"] != "manual_expense":
        return False
    conn.execute(
        "UPDATE pseudo_freee_vouchers SET deal_id = NULL, user_corrected_json = '' WHERE deal_id = ?",
        (deal_id,),
    )
    conn.execute("DELETE FROM pseudo_freee_deal_lines WHERE deal_id = ?", (deal_id,))
    conn.execute("DELETE FROM pseudo_freee_deals WHERE id = ?", (deal_id,))
    return True


def update_manual_expense(conn: db.Connection, deal_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """手入力経費の取引を更新する（手入力経費のみ。在庫連携の取引は編集不可）。"""
    existing = conn.execute("SELECT * FROM pseudo_freee_deals WHERE id = ?", (deal_id,)).fetchone()
    if not existing:
        raise ValueError("取引が見つかりません。")
    if existing["source_type"] != "manual_expense":
        raise ValueError("手入力経費のみ編集できます。")

    issue_date = required_text(data.get("issue_date"), "issue_date")
    partner_name = required_text(data.get("partner_name"), "partner_name")
    account_item_name = required_text(data.get("account_item_name"), "account_item_name")
    amount = to_float(data.get("amount"))
    if amount <= 0:
        raise ValueError("amount must be greater than 0")
    payment_method = str(data.get("payment_method", "現金") or "現金")
    if payment_method not in {"現金", "普通預金", "未払金"}:
        raise ValueError("payment_method must be 現金, 普通預金, or 未払金")
    due_date = str(data.get("due_date", "") or "")
    if payment_method != "未払金":
        due_date = ""
    tax_category = str(data.get("tax_category", "課税仕入 10%") or "課税仕入 10%")
    description = str(data.get("description", "") or account_item_name)
    memo = str(data.get("memo", "") or "")
    tax_rate = to_float(data.get("tax_rate"), 10)
    payload = {
        "source_app": "manual",
        "source_type": "manual_expense",
        "payload": {
            "api_target": "pseudo_freee_manual_expense",
            "issue_date": issue_date,
            "due_date": due_date,
            "type": "expense",
            "payment_method": payment_method,
            "partner_name": partner_name,
            "invoice_no": "",
            "memo": memo,
            "details": [
                {
                    "sku": "",
                    "description": description,
                    "quantity": 1,
                    "unit_price": amount,
                    "tax_rate": tax_rate,
                    "tax_category": tax_category,
                    "amount": amount,
                    "account_item_name": account_item_name,
                }
            ],
        },
    }
    conn.execute(
        """
        UPDATE pseudo_freee_deals
        SET issue_date = ?, due_date = ?, partner_name = ?, account_item_name = ?,
            tax_category = ?, amount = ?, memo = ?, payment_method = ?, payload_json = ?
        WHERE id = ?
        """,
        (
            issue_date,
            due_date,
            partner_name,
            account_item_name,
            tax_category,
            amount,
            memo,
            payment_method,
            json.dumps(payload, ensure_ascii=False, indent=2),
            deal_id,
        ),
    )
    conn.execute("DELETE FROM pseudo_freee_deal_lines WHERE deal_id = ?", (deal_id,))
    conn.execute(
        """
        INSERT INTO pseudo_freee_deal_lines (
            deal_id, sku, description, quantity, unit_price, tax_rate, tax_category, amount, account_item_name
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (deal_id, "", description, 1, amount, tax_rate, tax_category, amount, account_item_name),
    )
    remember_expense_masters(conn, partner_name, account_item_name, tax_category)
    return {"ok": True, "pseudo_freee_deal_id": deal_id}


# ---------------------------------------------------------------------------
# AI証憑入力（A-5 ステップ2: レシート画像 → AI下書き → 人が登録）
# ---------------------------------------------------------------------------
# 鉄則（EVOLUTION_PLAN.md）: AIは画像→下書き(ai_extracted_json)まで。
# 「登録」は人が経費フォームで行い、user_corrected_json と deal_id を残す（自動登録はしない）。
# 解析(ai_capture)は副作用なし。DB書き込みはこの app.py が単一の主体。


def _safe_filename(name: str) -> str:
    """元ファイル名を保存用に無害化（パス区切りを除去。空なら voucher）。"""
    base = Path(str(name or "")).name.strip().replace("\\", "").replace("/", "")
    return base or "voucher"


def store_voucher_image(file_name: str, data: bytes) -> str:
    """元画像を保存し、key（相対パス）を返す（DBにはこれだけ持つ）。

    内容ハッシュをファイル名に含めて重複保存を避ける。
    A-8: 実際の保存先は storage が決める（env で R2 / ローカルフォルダを自動切替）。
    """
    digest = hashlib.sha256(data).hexdigest()[:16]
    rel = f"{digest}_{_safe_filename(file_name)}"
    storage.save_bytes(VOUCHER_DIR, rel, data)
    return rel


def create_voucher(
    conn: db.Connection,
    *,
    file_name: str,
    mime_type: str,
    image_bytes: bytes,
    draft: dict[str, Any],
    content_hash: str = "",
) -> int:
    """証憑を保存する（AI下書きのみ。user_corrected_json は空＝未登録のまま）。"""
    storage_path = store_voucher_image(file_name, image_bytes)
    return db.insert_returning_id(
        conn,
        """
        INSERT INTO pseudo_freee_vouchers
            (file_name, storage_path, mime_type, content_hash, ai_extracted_json, confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _safe_filename(file_name),
            storage_path,
            mime_type,
            content_hash,
            json.dumps(draft, ensure_ascii=False),
            float(draft.get("overall_confidence", 0) or 0),
        ),
    )


def capture_expense(
    conn: db.Connection,
    *,
    file_name: str,
    mime_type: str,
    image_bytes: bytes,
) -> dict[str, Any]:
    """レシート画像 → AI下書き（登録しない）。画像と抽出結果を証憑として保存する。

    勘定科目・税区分は疑似freee のマスタ候補から選ばせる。
    """
    masters = list_expense_masters(conn)
    draft = ai_capture.analyze_voucher(
        image_bytes,
        mime_type,
        account_items=masters["account_items"],
        tax_categories=masters["tax_categories"],
    )
    # 重複検知: 同じ画像（内容ハッシュ）の証憑が既にあるか。あれば警告に使う（保存は止めない）。
    content_hash = hashlib.sha256(image_bytes).hexdigest()
    duplicate_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM pseudo_freee_vouchers WHERE content_hash = ? ORDER BY id", (content_hash,)
        ).fetchall()
    ]
    voucher_id = create_voucher(
        conn, file_name=file_name, mime_type=mime_type, image_bytes=image_bytes, draft=draft, content_hash=content_hash
    )
    return {
        "ok": True,
        "voucher_id": voucher_id,
        "draft": draft["fields"],
        "confidence": draft["confidence"],
        "overall_confidence": draft["overall_confidence"],
        "low_confidence_fields": draft["low_confidence_fields"],
        "source": draft["source"],
        "duplicate": bool(duplicate_ids),
        "duplicate_of": duplicate_ids,
    }


def _voucher_row(conn: db.Connection, voucher_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM pseudo_freee_vouchers WHERE id = ?", (voucher_id,)
    ).fetchone()
    return row_to_dict(row) if row else None


def _voucher_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    ai = json.loads(row["ai_extracted_json"] or "{}")
    corrected = json.loads(row["user_corrected_json"]) if row["user_corrected_json"] else None
    fields = ai.get("fields", {})
    return {
        "id": row["id"],
        "deal_id": row["deal_id"],
        "file_name": row["file_name"],
        "mime_type": row["mime_type"],
        "confidence": row["confidence"],
        "partner_name": fields.get("partner_name", ""),
        "amount": float(fields.get("amount") or 0),
        "account_item": fields.get("account_item", ""),
        "issue_date": fields.get("issue_date", ""),
        # deal_id が入る（人が登録した）と user_corrected_json も入る＝「取込済み」。
        "registered": corrected is not None,
        "created_at": row["created_at"],
        "ai_extracted": ai,
        "user_corrected": corrected,
        "low_confidence_fields": ai.get("low_confidence_fields", []),
        "image_url": f"/api/vouchers/{row['id']}/image",
    }


def list_vouchers(conn: db.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM pseudo_freee_vouchers ORDER BY id DESC"
    ).fetchall()
    return [_voucher_to_dict(row_to_dict(row)) for row in rows]


def voucher_detail(conn: db.Connection, voucher_id: int) -> dict[str, Any] | None:
    row = _voucher_row(conn, voucher_id)
    return _voucher_to_dict(row) if row else None


def link_voucher_to_deal(
    conn: db.Connection,
    voucher_id: int,
    deal_id: int,
    registered_fields: dict[str, Any] | None = None,
) -> None:
    """人が登録した経費伝票に証憑を紐付ける（取込済みの印＝deal_id + user_corrected_json）。"""
    row = _voucher_row(conn, voucher_id)
    if not row:
        return  # 存在しない voucher_id は黙ってスキップ（登録自体は成立させる）。
    payload = {"deal_id": deal_id, "fields": registered_fields or {}}
    conn.execute(
        "UPDATE pseudo_freee_vouchers SET deal_id = ?, user_corrected_json = ? WHERE id = ?",
        (deal_id, json.dumps(payload, ensure_ascii=False), voucher_id),
    )


def _maybe_link_voucher(conn: db.Connection, data: dict[str, Any], deal_id: int) -> None:
    raw = data.get("voucher_id")
    if not raw:
        return
    try:
        voucher_id = int(raw)
    except (TypeError, ValueError):
        return
    link_voucher_to_deal(
        conn,
        voucher_id,
        deal_id,
        {k: data.get(k) for k in ("issue_date", "partner_name", "account_item_name", "tax_category", "amount", "memo")},
    )


def delete_voucher(conn: db.Connection, voucher_id: int) -> bool:
    """証憑を削除する（DB行＋保存画像）。存在しなければ False。"""
    row = _voucher_row(conn, voucher_id)
    if not row:
        return False
    storage.delete(VOUCHER_DIR, row["storage_path"])  # A-8: 画像が消せなくても DB 行の削除は進める
    conn.execute("DELETE FROM pseudo_freee_vouchers WHERE id = ?", (voucher_id,))
    return True


def load_voucher_image(conn: db.Connection, voucher_id: int) -> tuple[bytes, str] | None:
    """証憑の元画像バイト列と MIME を返す。無ければ None。"""
    row = _voucher_row(conn, voucher_id)
    if not row:
        return None
    data = storage.read_bytes(VOUCHER_DIR, row["storage_path"])  # A-8: R2/ローカルを自動判定
    if data is None:
        return None
    return data, (row["mime_type"] or "application/octet-stream")


def deal_filters_from_query(query: str) -> dict[str, str]:
    params = parse_qs(query)
    return {
        "deal_type": params.get("deal_type", [""])[0],
        "source_type": params.get("source_type", [""])[0],
        "partner_query": params.get("partner_query", [""])[0].strip(),
        "date_from": params.get("date_from", [""])[0],
        "date_to": params.get("date_to", [""])[0],
    }


def list_deals(conn: db.Connection, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
    filters = filters or {}
    clauses: list[str] = []
    values: list[Any] = []
    if filters.get("deal_type") in {"income", "expense"}:
        clauses.append("deal_type = ?")
        values.append(filters["deal_type"])
    if filters.get("source_type") in {"purchase", "sale", "manual_expense"}:
        clauses.append("source_type = ?")
        values.append(filters["source_type"])
    if filters.get("partner_query"):
        clauses.append("partner_name LIKE ?")
        values.append(f"%{filters['partner_query']}%")
    if filters.get("date_from"):
        clauses.append("issue_date >= ?")
        values.append(filters["date_from"])
    if filters.get("date_to"):
        clauses.append("issue_date <= ?")
        values.append(filters["date_to"])
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"""
        SELECT *
        FROM pseudo_freee_deals
        {where_sql}
        ORDER BY issue_date DESC, id DESC
        """,
        values,
    ).fetchall()
    return [row_to_dict(row) for row in rows]


def get_deal(conn: db.Connection, deal_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM pseudo_freee_deals WHERE id = ?", (deal_id,)).fetchone()
    if not row:
        return None
    deal = row_to_dict(row)
    lines = conn.execute(
        """
        SELECT sku, description, quantity, unit_price, tax_rate, tax_category, amount, account_item_name
        FROM pseudo_freee_deal_lines
        WHERE deal_id = ?
        ORDER BY id
        """,
        (deal_id,),
    ).fetchall()
    deal["lines"] = [row_to_dict(line) for line in lines]
    return deal


def get_summary(conn: db.Connection) -> dict[str, Any]:
    month = datetime.now().strftime("%Y-%m")
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN deal_type = 'income' THEN amount ELSE 0 END), 0) AS monthly_income_total,
            COALESCE(SUM(CASE WHEN source_type = 'purchase' THEN amount ELSE 0 END), 0) AS monthly_purchase_total,
            COALESCE(SUM(CASE WHEN source_type = 'manual_expense' THEN amount ELSE 0 END), 0) AS monthly_manual_expense_total,
            COUNT(*) AS deal_count
        FROM pseudo_freee_deals
        WHERE substr(issue_date, 1, 7) = ?
        """,
        (month,),
    ).fetchone()
    schedule = conn.execute(
        """
        SELECT
            COALESCE(SUM(CASE WHEN deal_type = 'income' AND due_date >= ? THEN amount ELSE 0 END), 0) AS receivable_total,
            COALESCE(SUM(CASE WHEN deal_type = 'expense' AND due_date >= ? THEN amount ELSE 0 END), 0) AS payable_total
        FROM pseudo_freee_deals
        """
        ,
        (today, today),
    ).fetchone()
    all_count = conn.execute("SELECT COUNT(*) AS count FROM pseudo_freee_deals").fetchone()["count"]
    income_total = float(row["monthly_income_total"])
    purchase_total = float(row["monthly_purchase_total"])
    manual_expense_total = float(row["monthly_manual_expense_total"])
    return {
        "month": month,
        "income_total": income_total,
        "purchase_total": purchase_total,
        "manual_expense_total": manual_expense_total,
        "expense_total": purchase_total + manual_expense_total,
        "gross_profit": income_total - purchase_total - manual_expense_total,
        "receivable_total": float(schedule["receivable_total"]),
        "payable_total": float(schedule["payable_total"]),
        "monthly_deal_count": int(row["deal_count"]),
        "deal_count": int(all_count),
    }


def get_monthly_trends(conn: db.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            substr(issue_date, 1, 7) AS month,
            COALESCE(SUM(CASE WHEN deal_type = 'income' THEN amount ELSE 0 END), 0) AS income_total,
            COALESCE(SUM(CASE WHEN source_type = 'purchase' THEN amount ELSE 0 END), 0) AS purchase_total,
            COALESCE(SUM(CASE WHEN source_type = 'manual_expense' THEN amount ELSE 0 END), 0) AS manual_expense_total,
            COUNT(*) AS deal_count
        FROM pseudo_freee_deals
        WHERE issue_date != ''
        GROUP BY substr(issue_date, 1, 7)
        ORDER BY month DESC
        LIMIT 12
        """
    ).fetchall()
    trends = []
    for row in rows:
        income_total = float(row["income_total"])
        purchase_total = float(row["purchase_total"])
        manual_expense_total = float(row["manual_expense_total"])
        trends.append(
            {
                "month": row["month"],
                "income_total": income_total,
                "purchase_total": purchase_total,
                "manual_expense_total": manual_expense_total,
                "gross_profit": income_total - purchase_total - manual_expense_total,
                "deal_count": int(row["deal_count"]),
            }
        )
    return trends


# A-6: Clerk サインインのブートストラップ（在庫 index_html.py の方式を踏襲）。
# 公開キー pk_xxx の3要素目以降は frontend-api ホストの base64（末尾 '$' を除去）。
# 生文字列(r''')で正規表現のバックスラッシュをそのまま保つ。template literal(${})は使わない。
_PF_CLERK_BOOTSTRAP_JS = "<script>\n" + r'''(function(){
  var CFG = window.__PF_CONFIG__ || {};
  function feApi(pk){ var e = pk.split("_").slice(2).join("_"); try { return atob(e).replace(/\$$/, ""); } catch(_){ return ""; } }
  function loadClerk(pk){
    return new Promise(function(resolve, reject){
      var host = feApi(pk);
      if(!host){ reject(new Error("Clerk 公開キーの形式が不正です")); return; }
      var s = document.createElement("script");
      s.async = true; s.crossOrigin = "anonymous";
      s.setAttribute("data-clerk-publishable-key", pk);
      s.src = "https://" + host + "/npm/@clerk/clerk-js@5/dist/clerk.browser.js";
      s.onload = resolve;
      s.onerror = function(){ reject(new Error("Clerk スクリプトの読み込みに失敗しました")); };
      document.head.appendChild(s);
    });
  }
  function gate(){ document.documentElement.classList.add("pf-gated"); }
  function ungate(){ document.documentElement.classList.remove("pf-gated"); }
  async function boot(){
    if(!CFG.clerkConfigured || CFG.devMode){ ungate(); return; }  // dev/未設定は素通り
    gate();
    await loadClerk(CFG.clerkPublishableKey);
    await window.Clerk.load();
    function render(){
      if(window.Clerk.user){
        ungate();
        var u = document.getElementById("pf-clerk-user");
        if(u){ u.innerHTML = ""; window.Clerk.mountUserButton(u); }
      } else {
        gate();
        var g = document.getElementById("pf-clerk-signin");
        if(g && !g.hasChildNodes()){ window.Clerk.mountSignIn(g); }
      }
    }
    window.Clerk.addListener(render);
    render();
  }
  boot().catch(function(e){ var el = document.getElementById("pf-gate-msg"); if(el){ el.textContent = e.message; } });
})();''' + "\n</script>"


def render_page(title: str, body: str) -> bytes:
    # A-6: 在庫アプリと「同じ Clerk」でサインインゲートを掛ける（=同じログインで両アプリを使える）。
    # サーバ側は env から公開設定だけを読み、ブラウザ側(ClerkJS)がサインインを必須化する。
    # 疑似freee は外部システムのモックなので、画面(人が見る所)はゲートし、/api/deals(在庫からの
    # server-to-server 送信を受ける口)は機械向け API として開けておく（in auth.py に明記）。
    gate_config = {
        "clerkPublishableKey": auth.clerk_publishable_key(),
        "clerkConfigured": auth.clerk_configured(),
        "devMode": auth.auth_dev_mode(),
    }
    # </script> でテンプレが壊れないよう "/" をエスケープして埋め込む（在庫 index_html と同じ防御）。
    gate_config_json = json.dumps(gate_config, ensure_ascii=False).replace("</", "<\\/")
    gate_head = (
        "<script>window.__PF_CONFIG__ = " + gate_config_json + ";</script>\n"
        "<script>(function(){var c=window.__PF_CONFIG__||{};"
        # 本番(Clerk設定あり・devでない)は、本文が描画される前に伏せておく（未サインインの内容を一瞬も出さない）。
        "if(c.clerkConfigured&&!c.devMode){document.documentElement.classList.add('pf-gated');}})();</script>\n"
        "<style>\n"
        "  html.pf-gated body > header, html.pf-gated body > main { visibility: hidden; }\n"
        "  #pf-signin-gate { display: none; }\n"
        "  html.pf-gated #pf-signin-gate { display: flex; position: fixed; inset: 0; z-index: 50;\n"
        "    align-items: center; justify-content: center; background: rgba(15,23,42,.45); }\n"
        "  #pf-signin-gate .pf-gate-card { background: #fff; border: 1px solid #d9e2ec; border-radius: 12px;\n"
        "    padding: 24px; max-width: 460px; width: calc(100% - 32px); box-shadow: 0 18px 48px rgba(15,23,42,.22); }\n"
        "  #pf-signin-gate h2 { margin: 0 0 6px; font-size: 18px; }\n"
        "  #pf-signin-gate p { margin: 0 0 14px; color: #65758b; font-size: 13px; }\n"
        "  #pf-clerk-user { display: inline-flex; align-items: center; }\n"
        "</style>"
    )
    inventory_link = (
        f'<a href="{html.escape(INVENTORY_APP_URL)}/launcher">← アプリ入口へ</a> '
        if INVENTORY_APP_URL
        else ""
    )
    gate_body = (
        '<div id="pf-signin-gate"><div class="pf-gate-card">'
        "<h2>疑似freee にサインイン</h2>"
        "<p>在庫ダッシュボードと<strong>同じアカウント</strong>でサインインしてください。</p>"
        '<div id="pf-clerk-signin"></div>'
        '<p id="pf-gate-msg" style="color:#b91c1c;font-size:13px;"></p>'
        "</div></div>\n"
        + _PF_CLERK_BOOTSTRAP_JS
    )
    page = f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f8fb;
      --surface: #ffffff;
      --line: #d9e2ec;
      --text: #1d2733;
      --muted: #65758b;
      --income: #0f766e;
      --expense: #b45309;
      --accent: #2563eb;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      line-height: 1.55;
    }}
    header {{
      background: var(--surface);
      border-bottom: 1px solid var(--line);
    }}
    .wrap {{
      width: min(1180px, calc(100% - 32px));
      margin: 0 auto;
    }}
    .topbar {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px 0;
    }}
    h1 {{
      margin: 0;
      font-size: 22px;
      letter-spacing: 0;
    }}
    a {{ color: var(--accent); text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    main {{ padding: 24px 0 42px; }}
    .kpis {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}
    .card {{
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px 16px;
    }}
    .panel-grid {{
      display: grid;
      grid-template-columns: minmax(280px, 420px) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
      margin-bottom: 20px;
    }}
    .expense-form, .filters {{
      display: grid;
      gap: 10px;
    }}
    .form-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }}
    label {{
      display: grid;
      gap: 4px;
      color: var(--muted);
      font-size: 13px;
      font-weight: 600;
    }}
    input, select, textarea, button {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      background: #fff;
      color: var(--text);
      font: inherit;
    }}
    label.is-disabled {{
      color: #9ca3af;
    }}
    label.is-disabled select {{
      background: #f3f4f6;
      color: #9ca3af;
      cursor: not-allowed;
      opacity: 1;
    }}
    input::placeholder {{ color: #7a8798; opacity: 1; }}
    input.no-spinner::-webkit-outer-spin-button,
    input.no-spinner::-webkit-inner-spin-button {{
      -webkit-appearance: none;
      margin: 0;
    }}
    input.no-spinner {{
      appearance: textfield;
      -moz-appearance: textfield;
    }}
    .combo {{
      position: relative;
      display: grid;
      grid-template-columns: minmax(0, 1fr) 44px;
    }}
    .combo input {{
      border-top-right-radius: 0;
      border-bottom-right-radius: 0;
    }}
    .combo-toggle {{
      width: 44px;
      border-color: var(--line);
      border-left: 0;
      border-top-left-radius: 0;
      border-bottom-left-radius: 0;
      background: #fff;
      color: var(--text);
      padding: 0;
      font-size: 14px;
    }}
    .combo-menu {{
      position: absolute;
      z-index: 30;
      top: calc(100% + 4px);
      left: 0;
      right: 0;
      max-height: 260px;
      overflow: auto;
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 12px 28px rgba(29, 39, 51, 0.14);
      padding: 6px 0;
    }}
    .combo-menu[hidden] {{ display: none; }}
    .combo-option {{
      width: 100%;
      border: 0;
      border-radius: 0;
      background: #fff;
      color: var(--text);
      padding: 9px 12px;
      text-align: left;
      font-weight: 600;
    }}
    .combo-option:hover {{ background: #f0f5ff; }}
    .combo-empty {{
      padding: 9px 12px;
      color: var(--muted);
      font-size: 13px;
    }}
    textarea {{ min-height: 76px; resize: vertical; }}
    button {{
      border-color: var(--accent);
      background: var(--accent);
      color: #fff;
      cursor: pointer;
      font-weight: 700;
    }}
    .filter-row {{
      display: grid;
      grid-template-columns: 130px 160px minmax(180px, 1fr) 150px 150px 110px 86px;
      gap: 10px;
      align-items: end;
    }}
    .master-grid {{
      display: grid;
      grid-template-columns: 170px minmax(180px, 1fr) 260px 180px 120px;
      gap: 10px;
      align-items: end;
    }}
    .master-lists {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      margin-top: 16px;
    }}
    .master-list {{
      margin: 8px 0 0;
      padding: 0;
      list-style: none;
      color: var(--muted);
      font-size: 13px;
      max-height: 140px;
      overflow: auto;
    }}
    .master-list li {{ padding: 2px 0; }}
    .master-option {{
      width: 100%;
      border: 0;
      border-radius: 4px;
      padding: 4px 6px;
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font: inherit;
      font-weight: 400;
      text-align: left;
    }}
    .master-option:hover {{
      background: #f0f5ff;
      color: var(--text);
    }}
    .master-search-key {{
      color: #7a8798;
      font-size: 12px;
    }}
    .label {{ color: var(--muted); font-size: 13px; }}
    .value {{ font-size: 24px; font-weight: 700; margin-top: 4px; }}
    .toolbar {{
      display: flex;
      justify-content: space-between;
      align-items: end;
      gap: 16px;
      margin: 24px 0 10px;
    }}
    h2 {{ margin: 0; font-size: 18px; }}
    table {{
      width: 100%;
      border-collapse: collapse;
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
    }}
    th, td {{
      padding: 11px 12px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{ color: var(--muted); font-weight: 600; background: #fbfcfe; }}
    tr:last-child td {{ border-bottom: 0; }}
    .num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    .badge {{
      display: inline-block;
      min-width: 52px;
      border-radius: 999px;
      padding: 2px 9px;
      font-size: 12px;
      font-weight: 700;
      text-align: center;
    }}
    .income {{ background: #d9f4ef; color: var(--income); }}
    .expense {{ background: #fff0d6; color: var(--expense); }}
    .manual {{ background: #e8eefc; color: #24438f; }}
    .empty {{
      background: var(--surface);
      border: 1px dashed var(--line);
      border-radius: 8px;
      color: var(--muted);
      padding: 26px;
      text-align: center;
    }}
    .ai-capture {{
      display: grid;
      gap: 8px;
      margin-bottom: 12px;
      padding: 12px;
      border: 1px solid #c7d7ff;
      border-radius: 8px;
      background: #f5f8ff;
    }}
    .ai-capture h3 {{ margin: 0; font-size: 14px; color: #24438f; }}
    .dropzone {{
      border: 2px dashed #9bb4f0;
      border-radius: 8px;
      padding: 16px;
      text-align: center;
      color: var(--muted);
      font-size: 13px;
      cursor: pointer;
      background: #fff;
    }}
    .dropzone.dragover {{ background: #eaf0ff; border-color: var(--accent); color: var(--accent); }}
    .ai-status {{ font-size: 13px; color: var(--muted); min-height: 18px; }}
    .ai-status.error {{ color: #b91c1c; }}
    .ai-preview {{ display: none; gap: 10px; align-items: center; }}
    .ai-preview img {{ width: 72px; height: 72px; object-fit: cover; border-radius: 6px; border: 1px solid var(--line); }}
    label.low-confidence {{ color: var(--expense); }}
    label.low-confidence input,
    label.low-confidence select,
    label.low-confidence textarea {{ border-color: var(--expense); background: #fff8ee; }}
    .low-flag {{ color: var(--expense); font-weight: 700; font-size: 11px; }}
    .voucher-list {{ display: grid; gap: 10px; margin-top: 12px; }}
    .voucher-card {{
      display: grid;
      grid-template-columns: 72px minmax(0, 1fr) auto;
      gap: 12px;
      align-items: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      background: var(--surface);
    }}
    .voucher-card img {{ width: 72px; height: 72px; object-fit: cover; border-radius: 6px; border: 1px solid var(--line); }}
    .voucher-meta {{ font-size: 13px; color: var(--text); min-width: 0; }}
    .voucher-meta .muted {{ color: var(--muted); font-size: 12px; }}
    .voucher-del {{
      width: auto;
      border-color: #e2b4b4;
      background: #fff;
      color: #b91c1c;
      padding: 6px 12px;
      font-weight: 700;
    }}
    .status-pill {{
      display: inline-block;
      border-radius: 999px;
      padding: 1px 8px;
      font-size: 11px;
      font-weight: 700;
    }}
    .status-pill.done {{ background: #d9f4ef; color: var(--income); }}
    .status-pill.draft {{ background: #fdeccb; color: var(--expense); }}
    .receipt-preview-card {{ display: flex; flex-direction: column; }}
    .receipt-preview {{
      flex: 1;
      min-height: 420px;
      max-height: 80vh;
      display: flex;
      align-items: flex-start;
      justify-content: center;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fbfcfe;
      padding: 8px;
      overflow: auto;
    }}
    .receipt-preview img {{
      max-width: 100%;
      height: auto;
      border-radius: 4px;
      display: none;
    }}
    .receipt-preview-empty {{
      margin: auto;
      padding: 24px;
      color: var(--muted);
      font-size: 13px;
      text-align: center;
      line-height: 1.8;
    }}
    label.is-disabled input {{ background: #f3f4f6; color: #9ca3af; cursor: not-allowed; }}
    .ai-cancel {{
      width: auto;
      justify-self: start;
      border-color: #d1d5db;
      background: #fff;
      color: #4b5563;
      padding: 6px 14px;
      font-weight: 700;
    }}
    .ai-cancel:hover {{ background: #f3f4f6; }}
    .right-col {{ display: grid; gap: 16px; align-content: start; min-width: 0; }}
    .ai-dup-warning {{
      border: 1px solid #f0b4b4;
      background: #fdecec;
      color: #b91c1c;
      border-radius: 8px;
      padding: 9px 12px;
      font-size: 13px;
      font-weight: 700;
    }}
    .voucher-toggle {{
      width: auto;
      justify-self: start;
      margin-top: 2px;
      border-color: var(--line);
      background: #fff;
      color: var(--accent);
      padding: 6px 12px;
      font-weight: 700;
    }}
    .voucher-toggle:hover {{ background: #f0f5ff; }}
    .row-actions {{ display: flex; gap: 8px; align-items: center; white-space: nowrap; }}
    .row-actions form {{ margin: 0; }}
    .row-del {{
      width: auto;
      border-color: #e2b4b4;
      background: #fff;
      color: #b91c1c;
      padding: 4px 10px;
      font-size: 12px;
      font-weight: 700;
    }}
    .row-del:hover {{ background: #fdecec; }}
    .detail-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) minmax(280px, 420px);
      gap: 16px;
      align-items: start;
    }}
    dl {{
      display: grid;
      grid-template-columns: 150px 1fr;
      gap: 10px 14px;
      margin: 0;
    }}
    dt {{ color: var(--muted); }}
    dd {{ margin: 0; }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: #111827;
      color: #eef2ff;
      border-radius: 8px;
      padding: 14px;
      overflow: auto;
      max-height: 620px;
      font-size: 13px;
    }}
    @media (max-width: 760px) {{
      .detail-grid, .panel-grid, .form-grid, .filter-row, .master-grid, .master-lists {{ grid-template-columns: 1fr; }}
      .topbar, .toolbar {{ align-items: flex-start; flex-direction: column; }}
      table {{ display: block; overflow-x: auto; }}
      dl {{ grid-template-columns: 1fr; }}
    }}
  </style>
{gate_head}
</head>
<body>
  <header>
    <div class="wrap topbar">
      <h1>疑似freee会計ダッシュボード</h1>
      <nav>{inventory_link}<a href="/">取引一覧</a> <span class="label">/ API: POST /api/deals</span> <span id="pf-clerk-user"></span></nav>
    </div>
  </header>
  <main class="wrap">{body}</main>
{gate_body}
</body>
</html>"""
    return page.encode("utf-8")


def yen(value: Any) -> str:
    return f"¥{float(value):,.0f}"


def selected(current: str, value: str) -> str:
    return " selected" if current == value else ""


def combo_option(value: str, search_key: str = "") -> str:
    search_text = " ".join(part for part in [value, search_key] if part)
    return (
        f'<button type="button" class="combo-option" '
        f'data-value="{html.escape(value, quote=True)}" '
        f'data-search="{html.escape(search_text, quote=True)}">'
        f"{html.escape(value)}</button>"
    )


def deal_type_label(deal_type: str) -> str:
    return "収入" if deal_type == "income" else "支出"


def source_type_label(source_type: str) -> str:
    labels = {
        "purchase": "在庫仕入",
        "sale": "在庫売上",
        "manual_expense": "手入力経費",
    }
    return labels.get(source_type, source_type)


def render_index(filters: dict[str, str] | None = None) -> bytes:
    filters = filters or {}
    with db_connection() as conn:
        deals = list_deals(conn, filters)
        summary = get_summary(conn)
        trends = get_monthly_trends(conn)
        masters = list_expense_masters(conn)

    rows = ""
    for deal in deals:
        badge_class = "income" if deal["deal_type"] == "income" else "expense"
        if deal["source_type"] == "manual_expense":
            source_label = "手入力"
            queue_label = ""
        else:
            source_label = f'{source_type_label(deal["source_type"])} #{deal["source_id"]}'
            queue_label = f'<br><span class="label">queue #{deal["queue_id"]}</span>'
        # 操作: 手入力経費のみ 編集・削除可。在庫連携の取引（仕入/売上）は在庫ダッシュボードが
        # 唯一の正のため、ここでは編集も削除もしない（pseudo_freee 側で消すと在庫計算と食い違う）。
        if deal["source_type"] == "manual_expense":
            action_cell = (
                f'<div class="row-actions"><a href="/deals/{deal["id"]}/edit">編集</a>'
                f'<form method="post" action="/deals/{deal["id"]}/delete" '
                f"""onsubmit="return confirm('取引 #{deal["id"]} を削除しますか？');">"""
                f'<button type="submit" class="row-del">削除</button></form></div>'
            )
        else:
            action_cell = '<span class="label">在庫側で管理</span>'
        rows += f"""
        <tr>
          <td><a href="/deals/{deal["id"]}">#{deal["id"]}</a></td>
          <td>{html.escape(deal["issue_date"])}</td>
          <td><span class="badge {badge_class}">{deal_type_label(deal["deal_type"])}</span></td>
          <td>{html.escape(deal["partner_name"])}</td>
          <td>{html.escape(deal["account_item_name"])}</td>
          <td class="num">{yen(deal["amount"])}</td>
          <td>{html.escape(deal["tax_category"])}</td>
          <td>{html.escape(deal["due_date"])}</td>
          <td>{html.escape(source_label)}{queue_label}</td>
          <td>{html.escape(deal["created_at"])}</td>
          <td>{action_cell}</td>
        </tr>"""

    trend_rows = ""
    for trend in trends:
        trend_rows += f"""
        <tr>
          <td>{html.escape(trend["month"])}</td>
          <td class="num">{yen(trend["income_total"])}</td>
          <td class="num">{yen(trend["purchase_total"])}</td>
          <td class="num">{yen(trend["manual_expense_total"])}</td>
          <td class="num">{yen(trend["gross_profit"])}</td>
          <td class="num">{trend["deal_count"]}</td>
        </tr>"""

    payee_options = "".join(
        combo_option(row["payee_name"], row["search_key"]) for row in masters["payee_settings"]
    )
    account_item_options = "".join(
        combo_option(row["account_item_name"], row["search_key"]) for row in masters["account_item_settings"]
    )
    account_default_tax = {
        row["account_item_name"]: row["default_tax_category"]
        for row in masters["account_item_settings"]
    }
    account_default_tax_json = html.escape(json.dumps(account_default_tax, ensure_ascii=False), quote=False)
    tax_category_options = "".join(
        f'<option value="{html.escape(value, quote=True)}"{selected(value, "課税仕入 10%")}>{html.escape(value)}</option>'
        for value in masters["tax_categories"]
    )
    tax_category_master_options = "".join(
        f'<option value="{html.escape(value, quote=True)}">{html.escape(value)}</option>'
        for value in masters["tax_categories"]
    )
    partner_filter_options = "".join(
        f'<option value="{html.escape(value, quote=True)}"{selected(filters.get("partner_query", ""), value)}>{html.escape(value)}</option>'
        for value in masters["payees"]
    )
    payee_list = "".join(
        f"""
        <li><button type="button" class="master-option" data-master-option
          data-master-type="payee"
          data-name="{html.escape(row['payee_name'], quote=True)}"
          data-search-key="{html.escape(row['search_key'], quote=True)}">
          {html.escape(row['payee_name'])}{f' <span class="master-search-key">/ {html.escape(row["search_key"])}</span>' if row["search_key"] else ''}
        </button></li>"""
        for row in masters["payee_settings"]
    )
    account_item_list = "".join(
        f"""
        <li><button type="button" class="master-option" data-master-option
          data-master-type="account_item"
          data-name="{html.escape(row['account_item_name'], quote=True)}"
          data-search-key="{html.escape(row['search_key'], quote=True)}"
          data-default-tax-category="{html.escape(row['default_tax_category'], quote=True)}">
          {html.escape(row['account_item_name'])} / {html.escape(row['default_tax_category'])}{f' <span class="master-search-key">/ {html.escape(row["search_key"])}</span>' if row["search_key"] else ''}
        </button></li>"""
        for row in masters["account_item_settings"]
    )
    tax_category_list = "".join(f"<li>{html.escape(value)}</li>" for value in masters["tax_categories"])

    table = (
        f"""
        <table>
          <thead>
            <tr>
              <th>取引ID</th>
              <th>発生日</th>
              <th>区分</th>
              <th>取引先</th>
              <th>勘定科目</th>
              <th class="num">金額</th>
              <th>税区分</th>
              <th>支払/入金予定日</th>
              <th>送信元</th>
              <th>登録日時</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>"""
        if rows
        else '<div class="empty">条件に一致する取引はありません。</div>'
    )

    body = f"""
      <section class="kpis">
        <div class="card"><div class="label">対象月</div><div class="value">{html.escape(summary["month"])}</div></div>
        <div class="card"><div class="label">今月売上</div><div class="value">{yen(summary["income_total"])}</div></div>
        <div class="card"><div class="label">今月仕入</div><div class="value">{yen(summary["purchase_total"])}</div></div>
        <div class="card"><div class="label">手入力経費</div><div class="value">{yen(summary["manual_expense_total"])}</div></div>
        <div class="card"><div class="label">粗利</div><div class="value">{yen(summary["gross_profit"])}</div></div>
        <div class="card"><div class="label">未入金予定額</div><div class="value">{yen(summary["receivable_total"])}</div></div>
        <div class="card"><div class="label">未払予定額</div><div class="value">{yen(summary["payable_total"])}</div></div>
        <div class="card"><div class="label">登録取引数</div><div class="value">{summary["deal_count"]}</div></div>
      </section>
      <section class="panel-grid">
        <div class="card">
          <h2>経費入力</h2>
          <div class="ai-capture">
            <h3>📷 レシートをAIで読み取る</h3>
            <div class="dropzone" id="ai-dropzone" tabindex="0">
              画像を選択 / カメラで撮影 / ここにドラッグ＆ドロップ / 貼り付け(Ctrl+V)
              <input type="file" id="ai-file" accept="image/*" capture="environment" hidden>
            </div>
            <div class="ai-status" id="ai-status"></div>
            <div class="ai-dup-warning" id="ai-dup-warning" hidden></div>
            <div class="ai-preview" id="ai-preview">
              <img id="ai-preview-img" alt="読み取った画像">
              <div class="ai-status" id="ai-preview-meta"></div>
            </div>
            <button type="button" class="ai-cancel" id="ai-cancel" hidden>アップロードを取り消す</button>
          </div>
          <form class="expense-form" method="post" action="/manual-expenses">
            <input type="hidden" name="voucher_id" id="voucher-id-input" value="">
            <div class="form-grid">
              <label>発生日<input type="date" name="issue_date" value="{datetime.now().strftime("%Y-%m-%d")}" required></label>
              <label>支払方法
                <select name="payment_method" id="payment-method">
                  <option value="現金" selected>現金</option>
                  <option value="普通預金">普通預金</option>
                  <option value="未払金">未払金</option>
                </select>
              </label>
              <label>取引先
                <div class="combo" data-combo>
                  <input class="combo-input" name="partner_name" autocomplete="off" placeholder="検索して選択" required>
                  <button type="button" class="combo-toggle" aria-label="取引先候補を開閉" aria-expanded="false">▼</button>
                  <div class="combo-menu" hidden>{payee_options}<div class="combo-empty" hidden>候補がありません</div></div>
                </div>
              </label>
              <label>勘定科目
                <div class="combo" data-combo>
                  <input class="combo-input" name="account_item_name" autocomplete="off" placeholder="検索して選択" required>
                  <button type="button" class="combo-toggle" aria-label="勘定科目候補を開閉" aria-expanded="false">▼</button>
                  <div class="combo-menu" hidden>{account_item_options}<div class="combo-empty" hidden>候補がありません</div></div>
                </div>
              </label>
              <label>税区分<select name="tax_category">{tax_category_options}</select></label>
              <label>金額<input class="no-spinner" inputmode="decimal" name="amount" placeholder="例: 3300" required></label>
              <label id="due-date-label" class="is-disabled">支払予定日（未払金のとき）<input type="date" name="due_date" id="due-date-input" value="{datetime.now().strftime("%Y-%m-%d")}" disabled></label>
            </div>
            <label>摘要<input name="description" placeholder="例: 梱包資材"></label>
            <label>メモ<textarea name="memo"></textarea></label>
            <div class="label">新しい取引先・勘定科目・税区分は下のマスタ設定で追加してください。</div>
            <button type="submit">経費を登録</button>
          </form>
        </div>
        <div class="right-col">
          <div class="card receipt-preview-card">
            <div class="toolbar"><h2>レシートプレビュー</h2><span class="label">フォームと見比べて確認できます</span></div>
            <div class="receipt-preview" id="receipt-preview">
              <div class="receipt-preview-empty" id="receipt-preview-empty">読み取ったレシート画像がここに大きく表示されます。<br>左の入力内容と見比べてください。</div>
              <img id="receipt-preview-img" alt="レシートプレビュー">
            </div>
          </div>
          <div class="card">
            <div class="toolbar">
              <h2>証憑（レシート）一覧</h2>
              <span class="label">読み取った画像と下書き。登録で経費に紐付きます。</span>
            </div>
            <div class="voucher-list" id="voucher-list"></div>
          </div>
        </div>
      </section>
      <section>
        <div class="toolbar">
          <h2>取引一覧</h2>
          <span class="label">ローカルURL: http://127.0.0.1:{PORT}</span>
        </div>
        <form class="filters card" method="get" action="/">
          <div class="filter-row">
            <label>区分
              <select name="deal_type">
                <option value="">すべて</option>
                <option value="income"{selected(filters.get("deal_type", ""), "income")}>収入</option>
                <option value="expense"{selected(filters.get("deal_type", ""), "expense")}>支出</option>
              </select>
            </label>
            <label>送信元
              <select name="source_type">
                <option value="">すべて</option>
                <option value="sale"{selected(filters.get("source_type", ""), "sale")}>在庫売上</option>
                <option value="purchase"{selected(filters.get("source_type", ""), "purchase")}>在庫仕入</option>
                <option value="manual_expense"{selected(filters.get("source_type", ""), "manual_expense")}>手入力経費</option>
              </select>
            </label>
            <label>取引先
              <select name="partner_query">
                <option value="">すべて</option>
                {partner_filter_options}
              </select>
            </label>
            <label>開始日<input type="date" name="date_from" value="{html.escape(filters.get("date_from", ""), quote=True)}"></label>
            <label>終了日<input type="date" name="date_to" value="{html.escape(filters.get("date_to", ""), quote=True)}"></label>
            <button type="submit">検索</button>
            <a href="/">クリア</a>
          </div>
        </form>
        {table}
      </section>
      <section>
        <div class="toolbar"><h2>月次推移</h2></div>
        <table>
          <thead><tr><th>月</th><th class="num">売上</th><th class="num">仕入</th><th class="num">経費</th><th class="num">粗利</th><th class="num">件数</th></tr></thead>
          <tbody>{trend_rows or '<tr><td colspan="6">まだ月次データはありません。</td></tr>'}</tbody>
        </table>
      </section>
      <section class="card">
        <div class="toolbar">
          <h2>マスタ設定</h2>
          <span class="label">候補数: 取引先 {len(masters["payees"])} / 勘定科目 {len(masters["account_items"])} / 税区分 {len(masters["tax_categories"])}</span>
        </div>
        <form method="post" action="/expense-masters">
          <div class="master-grid">
            <label>種類
              <select name="master_type">
                <option value="payee">取引先</option>
                <option value="account_item">勘定科目</option>
              </select>
            </label>
            <label>名称<input name="name" required></label>
            <label>検索キー<input name="search_key" placeholder="例: shi / s / しいれ"></label>
            <label data-master-tax-field>標準税区分<select name="default_tax_category" disabled>{tax_category_master_options}</select></label>
            <button type="submit" data-master-submit>追加</button>
          </div>
        </form>
        <div class="master-lists">
          <div><strong>取引先</strong><ul class="master-list">{payee_list}</ul></div>
          <div><strong>勘定科目 / 標準税区分</strong><ul class="master-list">{account_item_list}</ul></div>
          <div><strong>税区分（固定候補）</strong><ul class="master-list">{tax_category_list}</ul></div>
        </div>
      </section>
      <script>
        function setupCombo(combo) {{
          const input = combo.querySelector(".combo-input");
          const toggle = combo.querySelector(".combo-toggle");
          const menu = combo.querySelector(".combo-menu");
          const options = Array.from(combo.querySelectorAll(".combo-option"));
          const empty = combo.querySelector(".combo-empty");

          function filterOptions() {{
            const query = input.value.trim().toLowerCase();
            let visibleCount = 0;
            for (const option of options) {{
              const searchText = (option.dataset.search || option.dataset.value).toLowerCase();
              const visible = searchText.includes(query);
              option.hidden = !visible;
              if (visible) visibleCount += 1;
            }}
            if (empty) empty.hidden = visibleCount !== 0;
          }}

          function setOpen(open) {{
            menu.hidden = !open;
            toggle.setAttribute("aria-expanded", String(open));
            toggle.textContent = open ? "▲" : "▼";
            if (open) filterOptions();
          }}

          toggle.addEventListener("click", event => {{
            event.preventDefault();
            event.stopPropagation();
            const willOpen = menu.hidden;
            setOpen(willOpen);
            if (willOpen) input.focus();
          }});

          input.addEventListener("input", () => {{
            filterOptions();
            setOpen(true);
          }});

          input.addEventListener("keydown", event => {{
            if (event.key === "Escape") setOpen(false);
            if (event.key === "ArrowDown") setOpen(true);
          }});

          for (const option of options) {{
            option.addEventListener("click", () => {{
              input.value = option.dataset.value;
              setOpen(false);
              input.dispatchEvent(new Event("change", {{ bubbles: true }}));
            }});
          }}

          document.addEventListener("click", event => {{
            if (!combo.contains(event.target)) setOpen(false);
          }});
        }}

        for (const combo of document.querySelectorAll("[data-combo]")) setupCombo(combo);

        const masterTypeSelect = document.querySelector("select[name='master_type']");
        const masterNameInput = document.querySelector("input[name='name']");
        const masterSearchKeyInput = document.querySelector("input[name='search_key']");
        const masterTaxField = document.querySelector("[data-master-tax-field]");
        const masterTaxSelect = masterTaxField ? masterTaxField.querySelector("select") : null;
        const masterSubmitButton = document.querySelector("[data-master-submit]");
        function setMasterMode(mode) {{
          if (masterSubmitButton) masterSubmitButton.textContent = mode === "update" ? "更新" : "追加";
        }}
        function syncMasterFields() {{
          if (masterTypeSelect && masterTaxField && masterTaxSelect) {{
            const enabled = masterTypeSelect.value === "account_item";
            masterTaxSelect.disabled = !enabled;
            masterTaxField.classList.toggle("is-disabled", !enabled);
          }}
        }}
        if (masterTypeSelect) {{
          masterTypeSelect.addEventListener("change", syncMasterFields);
          syncMasterFields();
        }}
        if (masterTypeSelect) masterTypeSelect.addEventListener("change", () => setMasterMode("add"));
        if (masterNameInput) masterNameInput.addEventListener("input", () => setMasterMode("add"));
        for (const option of document.querySelectorAll("[data-master-option]")) {{
          option.addEventListener("click", () => {{
            if (masterTypeSelect) masterTypeSelect.value = option.dataset.masterType;
            if (masterNameInput) masterNameInput.value = option.dataset.name || "";
            if (masterSearchKeyInput) masterSearchKeyInput.value = option.dataset.searchKey || "";
            if (masterTaxSelect && option.dataset.defaultTaxCategory) {{
              masterTaxSelect.value = option.dataset.defaultTaxCategory;
            }}
            syncMasterFields();
            setMasterMode("update");
            if (masterSearchKeyInput) masterSearchKeyInput.focus();
          }});
        }}

        const accountDefaultTax = JSON.parse(`{account_default_tax_json}`);
        const accountInput = document.querySelector("input[name='account_item_name']");
        const taxSelect = document.querySelector("select[name='tax_category']");
        if (accountInput && taxSelect) {{
          accountInput.addEventListener("change", () => {{
            const defaultTax = accountDefaultTax[accountInput.value];
            if (defaultTax) taxSelect.value = defaultTax;
          }});
        }}

        // --- A-5 ステップ2: レシートのAI読み取り（写真→下書き→人が登録） ---
        const aiDropzone = document.getElementById("ai-dropzone");
        const aiFile = document.getElementById("ai-file");
        const aiStatus = document.getElementById("ai-status");
        const aiPreview = document.getElementById("ai-preview");
        const aiPreviewImg = document.getElementById("ai-preview-img");
        const aiPreviewMeta = document.getElementById("ai-preview-meta");
        const voucherIdInput = document.getElementById("voucher-id-input");
        const voucherList = document.getElementById("voucher-list");
        const receiptPreviewImg = document.getElementById("receipt-preview-img");
        const receiptPreviewEmpty = document.getElementById("receipt-preview-empty");
        const aiDupWarning = document.getElementById("ai-dup-warning");
        let voucherExpanded = false;

        // 右側の大きなプレビューに画像を表示（フォームと見比べる用）。
        function showReceiptPreview(src) {{
          if (!receiptPreviewImg || !src) return;
          receiptPreviewImg.src = src;
          receiptPreviewImg.style.display = "block";
          if (receiptPreviewEmpty) receiptPreviewEmpty.style.display = "none";
        }}

        const paymentMethod = document.getElementById("payment-method");
        const dueDateInput = document.getElementById("due-date-input");
        const dueDateLabel = document.getElementById("due-date-label");
        const aiCancel = document.getElementById("ai-cancel");

        // 支払方法が「未払金」のときだけ支払予定日を有効化（現金/普通預金は即時決済＝予定日なし）。
        function syncDueDate() {{
          const enabled = paymentMethod && paymentMethod.value === "未払金";
          if (dueDateInput) dueDateInput.disabled = !enabled;
          if (dueDateLabel) dueDateLabel.classList.toggle("is-disabled", !enabled);
        }}
        if (paymentMethod) {{
          paymentMethod.addEventListener("change", syncDueDate);
          syncDueDate();
        }}

        // アップロードを取り消す: 下書きの証憑を削除し、フォームとプレビューを初期状態へ戻す。
        async function cancelCapture() {{
          const vid = voucherIdInput ? voucherIdInput.value : "";
          if (vid) {{
            try {{ await fetch(`/api/vouchers/${{vid}}`, {{ method: "DELETE" }}); }} catch (e) {{}}
          }}
          if (voucherIdInput) voucherIdInput.value = "";
          clearLowFlags();
          const form = document.querySelector(".expense-form");
          if (form) form.reset();
          syncDueDate();
          if (aiPreview) aiPreview.style.display = "none";
          if (receiptPreviewImg) {{ receiptPreviewImg.style.display = "none"; receiptPreviewImg.removeAttribute("src"); }}
          if (receiptPreviewEmpty) receiptPreviewEmpty.style.display = "";
          setStatus("", false);
          if (aiDupWarning) aiDupWarning.hidden = true;
          if (aiCancel) aiCancel.hidden = true;
          if (aiFile) aiFile.value = "";  // 同じファイルを選び直せるようにする
          loadVouchers();
        }}
        if (aiCancel) aiCancel.addEventListener("click", cancelCapture);

        // AIの項目名 → 経費フォームの input/select 名。
        const FIELD_TO_NAME = {{
          issue_date: "issue_date",
          partner_name: "partner_name",
          amount: "amount",
          tax_category: "tax_category",
          account_item: "account_item_name",
          memo: "memo",
        }};

        function setStatus(message, isError) {{
          if (!aiStatus) return;
          aiStatus.textContent = message;
          aiStatus.classList.toggle("error", !!isError);
        }}

        function fieldElement(formName) {{
          return document.querySelector(`.expense-form [name="${{formName}}"]`);
        }}

        function clearLowFlags() {{
          for (const label of document.querySelectorAll("label.low-confidence")) {{
            label.classList.remove("low-confidence");
          }}
          for (const flag of document.querySelectorAll(".low-flag")) flag.remove();
        }}

        function fillForm(draft, lowFields) {{
          clearLowFlags();
          for (const [aiName, formName] of Object.entries(FIELD_TO_NAME)) {{
            const el = fieldElement(formName);
            if (!el) continue;
            let value = draft[aiName];
            if (aiName === "amount") value = Math.round(Number(value) || 0);
            el.value = value == null ? "" : value;
            el.dispatchEvent(new Event("change", {{ bubbles: true }}));
          }}
          // 税区分はAIの値で上書き（勘定科目changeで既定税区分が入るため最後に設定）。
          const taxEl = fieldElement("tax_category");
          if (taxEl && draft.tax_category) taxEl.value = draft.tax_category;
          // 低信頼度の項目を色付け＋「要確認」表示（人の確認を促す）。
          for (const aiName of (lowFields || [])) {{
            const el = fieldElement(FIELD_TO_NAME[aiName]);
            if (!el) continue;
            const label = el.closest("label");
            if (!label) continue;
            label.classList.add("low-confidence");
            const flag = document.createElement("span");
            flag.className = "low-flag";
            flag.textContent = " 要確認";
            label.appendChild(flag);
          }}
        }}

        async function captureImage(file) {{
          if (!file) return;
          setStatus("AIが読み取り中…", false);
          let dataUrl;
          try {{
            dataUrl = await new Promise((resolve, reject) => {{
              const reader = new FileReader();
              reader.onload = () => resolve(String(reader.result));
              reader.onerror = () => reject(reader.error);
              reader.readAsDataURL(file);
            }});
          }} catch (err) {{
            setStatus("画像の読み込みに失敗しました", true);
            return;
          }}
          const base64 = dataUrl.includes(",") ? dataUrl.split(",")[1] : dataUrl;
          try {{
            const res = await fetch("/api/expense-capture", {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{
                file_name: file.name || "receipt",
                mime_type: file.type || "image/jpeg",
                image_base64: base64,
              }}),
            }});
            const result = await res.json();
            if (!res.ok || !result.ok) throw new Error(result.error || "読み取りに失敗しました");
            fillForm(result.draft, result.low_confidence_fields);
            if (voucherIdInput) voucherIdInput.value = result.voucher_id;
            const pct = Math.round((result.overall_confidence || 0) * 100);
            const sourceLabel = result.source === "anthropic" ? "Claude" : "お試しモード";
            setStatus(`読み取り完了（${{sourceLabel}}・全体信頼度 ${{pct}}%）。内容を確認して「経費を登録」を押してください。`, false);
            if (aiPreview) {{
              aiPreview.style.display = "flex";
              if (aiPreviewImg) aiPreviewImg.src = dataUrl;
              if (aiPreviewMeta) {{
                const amountText = Math.round(Number(result.draft.amount) || 0).toLocaleString();
                aiPreviewMeta.textContent = `${{result.draft.partner_name || "(支払先不明)"}} / ¥${{amountText}}`;
              }}
            }}
            showReceiptPreview(dataUrl);  // 右の大きなプレビューにも表示。
            if (aiCancel) aiCancel.hidden = false;  // 取り消しボタンを出す。
            if (aiDupWarning) {{
              if (result.duplicate) {{
                const n = (result.duplicate_of || []).length;
                aiDupWarning.textContent = `⚠️ 重複注意：同じレシート画像がすでに ${{n}}件 あります。二重登録になっていないか確認してください。`;
                aiDupWarning.hidden = false;
              }} else {{
                aiDupWarning.hidden = true;
              }}
            }}
            loadVouchers();
          }} catch (err) {{
            setStatus(err.message || "読み取りに失敗しました", true);
          }}
        }}

        if (aiDropzone && aiFile) {{
          aiDropzone.addEventListener("click", () => aiFile.click());
          aiDropzone.addEventListener("keydown", event => {{
            if (event.key === "Enter" || event.key === " ") {{ event.preventDefault(); aiFile.click(); }}
          }});
          aiFile.addEventListener("change", () => {{
            if (aiFile.files && aiFile.files[0]) captureImage(aiFile.files[0]);
          }});
          aiDropzone.addEventListener("dragover", event => {{
            event.preventDefault();
            aiDropzone.classList.add("dragover");
          }});
          aiDropzone.addEventListener("dragleave", () => aiDropzone.classList.remove("dragover"));
          aiDropzone.addEventListener("drop", event => {{
            event.preventDefault();
            aiDropzone.classList.remove("dragover");
            const file = event.dataTransfer && event.dataTransfer.files[0];
            if (file) captureImage(file);
          }});
          window.addEventListener("paste", event => {{
            const items = event.clipboardData && event.clipboardData.items;
            if (!items) return;
            for (const item of items) {{
              if (item.type && item.type.startsWith("image/")) {{
                const file = item.getAsFile();
                if (file) {{ captureImage(file); break; }}
              }}
            }}
          }});
        }}

        function escapeHtml(text) {{
          const div = document.createElement("div");
          div.textContent = text == null ? "" : String(text);
          return div.innerHTML;
        }}

        const VOUCHER_COLLAPSE_LIMIT = 3;
        async function loadVouchers() {{
          if (!voucherList) return;
          try {{
            const res = await fetch("/api/vouchers");
            const result = await res.json();
            const vouchers = (result && result.vouchers) || [];
            if (!vouchers.length) {{
              voucherList.innerHTML = '<div class="empty">まだ証憑はありません。上のエリアからレシート画像を読み取ってください。</div>';
              return;
            }}
            // 行が多いときは折りたたむ（既定は先頭の数件のみ表示）。
            const shown = voucherExpanded ? vouchers : vouchers.slice(0, VOUCHER_COLLAPSE_LIMIT);
            voucherList.innerHTML = shown.map(v => {{
              const pct = Math.round((v.confidence || 0) * 100);
              const status = v.registered
                ? '<span class="status-pill done">登録済み</span>'
                : '<span class="status-pill draft">下書き</span>';
              const amount = "¥" + (Math.round(Number(v.amount) || 0)).toLocaleString();
              const link = v.deal_id ? ` / <a href="/deals/${{v.deal_id}}">取引 #${{v.deal_id}}</a>` : "";
              return `
                <div class="voucher-card">
                  <img src="${{v.image_url}}" alt="証憑">
                  <div class="voucher-meta">
                    <div>${{escapeHtml(v.partner_name || "(支払先不明)")}} ・ ${{amount}} ${{status}}</div>
                    <div class="muted">${{escapeHtml(v.account_item || "")}} ${{escapeHtml(v.issue_date || "")}} ・ 信頼度 ${{pct}}%${{link}}</div>
                  </div>
                  <button type="button" class="voucher-del" data-voucher-id="${{v.id}}">削除</button>
                </div>`;
            }}).join("");
            for (const button of voucherList.querySelectorAll(".voucher-del")) {{
              button.addEventListener("click", () => deleteVoucher(button.dataset.voucherId));
            }}
            // 一覧の画像をクリックすると、右の大きなプレビューに表示（過去の証憑も見比べられる）。
            for (const img of voucherList.querySelectorAll(".voucher-card img")) {{
              img.style.cursor = "zoom-in";
              img.addEventListener("click", () => showReceiptPreview(img.getAttribute("src")));
            }}
            if (vouchers.length > VOUCHER_COLLAPSE_LIMIT) {{
              const toggle = document.createElement("button");
              toggle.type = "button";
              toggle.className = "voucher-toggle";
              toggle.textContent = voucherExpanded ? "折りたたむ" : `すべて表示（${{vouchers.length}}件）`;
              toggle.addEventListener("click", () => {{ voucherExpanded = !voucherExpanded; loadVouchers(); }});
              voucherList.appendChild(toggle);
            }}
          }} catch (err) {{
            voucherList.innerHTML = '<div class="empty">証憑一覧の取得に失敗しました。</div>';
          }}
        }}

        async function deleteVoucher(id) {{
          if (!id) return;
          if (!window.confirm("この証憑を削除しますか？")) return;
          try {{
            const res = await fetch(`/api/vouchers/${{id}}`, {{ method: "DELETE" }});
            const result = await res.json();
            if (!res.ok || !result.ok) throw new Error(result.error || "削除に失敗しました");
            loadVouchers();
          }} catch (err) {{
            window.alert(err.message || "削除に失敗しました");
          }}
        }}

        loadVouchers();
      </script>
    """
    return render_page("疑似freee会計ダッシュボード", body)


def render_detail(deal_id: int) -> bytes | None:
    with db_connection() as conn:
        deal = get_deal(conn, deal_id)
    if not deal:
        return None

    deal_type_label = "収入" if deal["deal_type"] == "income" else "支出"
    if deal["source_type"] == "manual_expense":
        source_label = "manual / 手入力経費"
        queue_label = ""
    else:
        source_label = f'{deal["source_app"]} / {source_type_label(deal["source_type"])} #{deal["source_id"]}'
        queue_label = str(deal["queue_id"] or "")
    line_rows = ""
    for line in deal["lines"]:
        line_rows += f"""
        <tr>
          <td>{html.escape(line["sku"])}</td>
          <td>{html.escape(line["description"])}</td>
          <td class="num">{line["quantity"]:g}</td>
          <td class="num">{yen(line["unit_price"])}</td>
          <td class="num">{line["tax_rate"]:g}%</td>
          <td>{html.escape(line["tax_category"])}</td>
          <td>{html.escape(line["account_item_name"])}</td>
          <td class="num">{yen(line["amount"])}</td>
        </tr>"""

    body = f"""
      <div class="toolbar">
        <h2>取引詳細 #{deal["id"]}</h2>
        <a href="/">一覧へ戻る</a>
      </div>
      <section class="detail-grid">
        <div>
          <div class="card">
            <dl>
              <dt>区分</dt><dd>{deal_type_label}</dd>
              <dt>発生日</dt><dd>{html.escape(deal["issue_date"])}</dd>
              <dt>支払/入金予定日</dt><dd>{html.escape(deal["due_date"])}</dd>
              <dt>取引先</dt><dd>{html.escape(deal["partner_name"])}</dd>
              <dt>取引先マスタID</dt><dd>{html.escape(str(deal["partner_master_id"] or ""))}</dd>
              <dt>freee取引先ID</dt><dd>{html.escape(deal["freee_partner_id"])}</dd>
              <dt>請求書/注文番号</dt><dd>{html.escape(deal["invoice_no"])}</dd>
              <dt>勘定科目</dt><dd>{html.escape(deal["account_item_name"])}</dd>
              <dt>税区分</dt><dd>{html.escape(deal["tax_category"])}</dd>
              <dt>金額</dt><dd>{yen(deal["amount"])}</dd>
              <dt>支払方法</dt><dd>{html.escape(deal.get("payment_method", "") or "—")}</dd>
              <dt>メモ</dt><dd>{html.escape(deal["memo"])}</dd>
              <dt>送信元</dt><dd>{html.escape(source_label)}</dd>
              <dt>キューID</dt><dd>{html.escape(queue_label)}</dd>
              <dt>登録日時</dt><dd>{html.escape(deal["created_at"])}</dd>
            </dl>
          </div>
          <div class="toolbar"><h2>明細</h2></div>
          <table>
            <thead>
              <tr>
                <th>SKU</th><th>摘要</th><th class="num">数量</th><th class="num">単価</th>
                <th class="num">税率</th><th>税区分</th><th>勘定科目</th><th class="num">金額</th>
              </tr>
            </thead>
            <tbody>{line_rows}</tbody>
          </table>
        </div>
        <aside>
          <div class="toolbar"><h2>受信/登録JSON</h2></div>
          <pre>{html.escape(deal["payload_json"])}</pre>
        </aside>
      </section>
    """
    return render_page(f"取引詳細 #{deal_id}", body)


def render_edit_deal(deal_id: int) -> bytes | None:
    """手入力経費の編集フォーム。手入力経費でなければ None（編集不可）。"""
    with db_connection() as conn:
        deal = get_deal(conn, deal_id)
        masters = list_expense_masters(conn)
    if not deal or deal["source_type"] != "manual_expense":
        return None

    description = deal["lines"][0]["description"] if deal.get("lines") else ""
    pm = deal.get("payment_method") or "現金"
    amt = float(deal["amount"])
    amount_text = str(int(amt)) if amt.is_integer() else str(amt)

    def with_current(values: list[str], current: str) -> list[str]:
        # 現在値がマスタ候補に無くても選べるよう先頭に足す。
        return ([current] + list(values)) if current and current not in values else list(values)

    def opts(values: list[str], current: str) -> str:
        return "".join(
            f'<option value="{html.escape(v, quote=True)}"{selected(current, v)}>{html.escape(v)}</option>'
            for v in values
        )

    pm_opts = "".join(f'<option value="{m}"{selected(pm, m)}>{m}</option>' for m in ("現金", "普通預金", "未払金"))
    due_disabled = "" if pm == "未払金" else " disabled"
    due_label_class = "" if pm == "未払金" else "is-disabled"

    body = f"""
      <div class="toolbar">
        <h2>取引 #{deal['id']} を編集（手入力経費）</h2>
        <a href="/">一覧へ戻る</a>
      </div>
      <section class="card" style="max-width: 560px;">
        <form class="expense-form" method="post" action="/deals/{deal['id']}">
          <div class="form-grid">
            <label>発生日<input type="date" name="issue_date" value="{html.escape(deal['issue_date'], quote=True)}" required></label>
            <label>支払方法<select name="payment_method" id="payment-method">{pm_opts}</select></label>
            <label>取引先<select name="partner_name" required>{opts(with_current(masters['payees'], deal['partner_name']), deal['partner_name'])}</select></label>
            <label>勘定科目<select name="account_item_name" required>{opts(with_current(masters['account_items'], deal['account_item_name']), deal['account_item_name'])}</select></label>
            <label>税区分<select name="tax_category">{opts(with_current(masters['tax_categories'], deal['tax_category']), deal['tax_category'])}</select></label>
            <label>金額<input class="no-spinner" inputmode="decimal" name="amount" value="{amount_text}" required></label>
            <label id="due-date-label" class="{due_label_class}">支払予定日（未払金のとき）<input type="date" name="due_date" id="due-date-input" value="{html.escape(deal['due_date'], quote=True)}"{due_disabled}></label>
          </div>
          <label>摘要<input name="description" value="{html.escape(description, quote=True)}"></label>
          <label>メモ<textarea name="memo">{html.escape(deal['memo'])}</textarea></label>
          <button type="submit">更新する</button>
        </form>
      </section>
      <script>
        const pm = document.getElementById("payment-method");
        const dd = document.getElementById("due-date-input");
        const ddl = document.getElementById("due-date-label");
        function sync() {{
          const en = pm && pm.value === "未払金";
          if (dd) dd.disabled = !en;
          if (ddl) ddl.classList.toggle("is-disabled", !en);
        }}
        if (pm) {{ pm.addEventListener("change", sync); sync(); }}
      </script>
    """
    return render_page(f"取引 #{deal_id} を編集", body)


class PseudoFreeeHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {format % args}")

    def send_cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def respond_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def respond_html(self, body: bytes, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_bytes(self, body: bytes, media_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.end_headers()

    def read_form(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw_body = self.rfile.read(length).decode("utf-8")
        params = parse_qs(raw_body)
        return {key: values[0] if values else "" for key, values in params.items()}

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in {"/", "/deals"}:
                self.respond_html(render_index(deal_filters_from_query(parsed.query)))
            elif parsed.path.startswith("/deals/") and parsed.path.endswith("/edit"):
                deal_id = to_int(parsed.path[len("/deals/"):-len("/edit")], "deal_id")
                body = render_edit_deal(deal_id)
                if body is None:
                    self.respond_json({"ok": False, "error": "deal not editable"}, HTTPStatus.NOT_FOUND)
                else:
                    self.respond_html(body)
            elif parsed.path.startswith("/deals/"):
                deal_id = to_int(parsed.path.removeprefix("/deals/"), "deal_id")
                body = render_detail(deal_id)
                if body is None:
                    self.respond_json({"ok": False, "error": "deal not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.respond_html(body)
            elif parsed.path == "/api/deals":
                with db_connection() as conn:
                    self.respond_json({"ok": True, "deals": list_deals(conn, deal_filters_from_query(parsed.query))})
            elif parsed.path == "/api/expense-masters":
                with db_connection() as conn:
                    self.respond_json({"ok": True, **list_expense_masters(conn)})
            elif parsed.path == "/api/vouchers":
                with db_connection() as conn:
                    self.respond_json({"ok": True, "vouchers": list_vouchers(conn)})
            elif parsed.path.endswith("/image") and parsed.path.startswith("/api/vouchers/"):
                voucher_id = to_int(parsed.path.removeprefix("/api/vouchers/").removesuffix("/image"), "voucher_id")
                with db_connection() as conn:
                    image = load_voucher_image(conn, voucher_id)
                if image is None:
                    self.respond_json({"ok": False, "error": "voucher image not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.respond_bytes(image[0], image[1])
            elif parsed.path.startswith("/api/vouchers/"):
                voucher_id = to_int(parsed.path.removeprefix("/api/vouchers/"), "voucher_id")
                with db_connection() as conn:
                    voucher = voucher_detail(conn, voucher_id)
                if not voucher:
                    self.respond_json({"ok": False, "error": "voucher not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.respond_json({"ok": True, "voucher": voucher})
            elif parsed.path.startswith("/api/deals/"):
                deal_id = to_int(parsed.path.removeprefix("/api/deals/"), "deal_id")
                with db_connection() as conn:
                    deal = get_deal(conn, deal_id)
                if not deal:
                    self.respond_json({"ok": False, "error": "deal not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.respond_json({"ok": True, "deal": deal})
            elif parsed.path == "/api/health":
                self.respond_json({"ok": True, "service": "pseudo_freee"})
            else:
                self.respond_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/deals":
                data = parse_json_body(self)
                with db_connection() as conn:
                    deal_id, created = create_deal(conn, data)
                status = HTTPStatus.CREATED if created else HTTPStatus.OK
                self.respond_json(
                    {
                        "ok": True,
                        "pseudo_freee_deal_id": deal_id,
                        "created": created,
                        "duplicate": not created,
                    },
                    status,
                )
            elif parsed.path == "/api/manual-expenses":
                data = parse_json_body(self)
                with db_connection() as conn:
                    result = create_manual_expense(conn, data)
                self.respond_json(result, HTTPStatus.CREATED)
            elif parsed.path == "/api/expense-capture":
                # レシート画像(base64) → AI下書き → 証憑保存。**登録はしない**（登録は人が経費フォームで行う）。
                data = parse_json_body(self)
                image_base64 = str(data.get("image_base64", "") or "")
                if not image_base64:
                    raise ValueError("image_base64 is required")
                try:
                    image_bytes = base64.b64decode(image_base64, validate=False)
                except Exception as exc:  # noqa: BLE001 - 不正な base64
                    raise ValueError("invalid image_base64") from exc
                if not image_bytes:
                    raise ValueError("画像がありません。")
                with db_connection() as conn:
                    result = capture_expense(
                        conn,
                        file_name=str(data.get("file_name", "") or ""),
                        mime_type=str(data.get("mime_type", "") or ""),
                        image_bytes=image_bytes,
                    )
                self.respond_json(result, HTTPStatus.CREATED)
            elif parsed.path == "/api/expense-masters":
                data = parse_json_body(self)
                with db_connection() as conn:
                    result = create_expense_master(conn, data)
                self.respond_json(result, HTTPStatus.CREATED)
            elif parsed.path == "/manual-expenses":
                data = self.read_form()
                with db_connection() as conn:
                    create_manual_expense(conn, data)
                self.redirect("/")
            elif parsed.path == "/expense-masters":
                data = self.read_form()
                with db_connection() as conn:
                    create_expense_master(conn, data)
                self.redirect("/")
            elif parsed.path.startswith("/deals/") and parsed.path.endswith("/delete"):
                deal_id = to_int(parsed.path[len("/deals/"):-len("/delete")], "deal_id")
                with db_connection() as conn:
                    delete_deal(conn, deal_id)
                self.redirect("/")
            elif parsed.path.startswith("/deals/"):
                # 取引の更新（手入力経費の編集フォームから）。
                deal_id = to_int(parsed.path.removeprefix("/deals/"), "deal_id")
                data = self.read_form()
                with db_connection() as conn:
                    update_manual_expense(conn, deal_id, data)
                self.redirect("/")
            else:
                self.respond_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/vouchers/"):
                voucher_id = to_int(parsed.path.removeprefix("/api/vouchers/"), "voucher_id")
                with db_connection() as conn:
                    deleted = delete_voucher(conn, voucher_id)
                if not deleted:
                    self.respond_json({"ok": False, "error": "voucher not found"}, HTTPStatus.NOT_FOUND)
                else:
                    self.respond_json({"ok": True, "deleted_voucher_id": voucher_id})
            else:
                self.respond_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.respond_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), PseudoFreeeHandler)
    print(f"Pseudo freee is running at http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping pseudo freee")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
