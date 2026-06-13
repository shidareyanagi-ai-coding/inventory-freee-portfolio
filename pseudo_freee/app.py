from __future__ import annotations

import html
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "pseudo_freee.db"
HOST = "127.0.0.1"
PORT = 8010


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

SCHEMA_SQL = f"""
PRAGMA foreign_keys = ON;

{DEALS_TABLE_SQL}

{LINES_TABLE_SQL}

{INDEX_SQL}

{MASTER_SQL}
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


@contextmanager
def db_connection() -> Any:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def deal_schema_needs_migration(conn: sqlite3.Connection) -> bool:
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


def migrate_deals_schema(conn: sqlite3.Connection) -> None:
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


def ensure_master_schema(conn: sqlite3.Connection) -> None:
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


def default_tax_for_account_item(account_item_name: str) -> str:
    for tax_category, account_items in DEFAULT_ACCOUNT_ITEM_TAX_CATEGORIES.items():
        if account_item_name in account_items:
            return tax_category
    return "課税仕入 10%"


def seed_master_data(conn: sqlite3.Connection) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO pseudo_freee_payees (payee_name) VALUES (?)",
        [(name,) for name in DEFAULT_PAYEES],
    )
    conn.executemany(
        """
        INSERT OR IGNORE INTO pseudo_freee_account_items (account_item_name, default_tax_category)
        VALUES (?, ?)
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
        "INSERT OR IGNORE INTO pseudo_freee_tax_categories (tax_category) VALUES (?)",
        [(name,) for name in DEFAULT_TAX_CATEGORIES],
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO pseudo_freee_payees (payee_name)
        SELECT DISTINCT partner_name
        FROM pseudo_freee_deals
        WHERE partner_name != ''
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO pseudo_freee_account_items (account_item_name)
        SELECT DISTINCT account_item_name
        FROM pseudo_freee_deals
        WHERE account_item_name != ''
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO pseudo_freee_tax_categories (tax_category)
        SELECT DISTINCT tax_category
        FROM pseudo_freee_deals
        WHERE tax_category != ''
        """
    )


def init_db() -> None:
    with db_connection() as conn:
        migrate_deals_schema(conn)
        conn.executescript(SCHEMA_SQL)
        ensure_master_schema(conn)
        seed_master_data(conn)


def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
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
    conn: sqlite3.Connection,
    payee_name: str,
    account_item_name: str,
    tax_category: str,
) -> None:
    if payee_name:
        conn.execute("INSERT OR IGNORE INTO pseudo_freee_payees (payee_name) VALUES (?)", (payee_name,))
    if account_item_name:
        conn.execute(
            """
            INSERT OR IGNORE INTO pseudo_freee_account_items (account_item_name, default_tax_category)
            VALUES (?, ?)
            """,
            (account_item_name, tax_category or "課税仕入 10%"),
        )
    if tax_category:
        conn.execute(
            "INSERT OR IGNORE INTO pseudo_freee_tax_categories (tax_category) VALUES (?)",
            (tax_category,),
        )


def list_expense_masters(conn: sqlite3.Connection) -> dict[str, Any]:
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


def create_expense_master(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
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
            "INSERT OR IGNORE INTO pseudo_freee_tax_categories (tax_category) VALUES (?)",
            (default_tax_category,),
        )
    elif master_type == "tax_category":
        conn.execute("INSERT OR IGNORE INTO pseudo_freee_tax_categories (tax_category) VALUES (?)", (name,))
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


def create_deal(conn: sqlite3.Connection, data: dict[str, Any]) -> tuple[int, bool]:
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

    cursor = conn.execute(
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
    deal_id = int(cursor.lastrowid)
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


def create_manual_expense(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    issue_date = required_text(data.get("issue_date"), "issue_date")
    partner_name = required_text(data.get("partner_name"), "partner_name")
    account_item_name = required_text(data.get("account_item_name"), "account_item_name")
    amount = to_float(data.get("amount"))
    if amount <= 0:
        raise ValueError("amount must be greater than 0")

    due_date = str(data.get("due_date", "") or "")
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

    cursor = conn.execute(
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
            json.dumps(payload, ensure_ascii=False, indent=2),
        ),
    )
    deal_id = int(cursor.lastrowid)
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
    return {"ok": True, "pseudo_freee_deal_id": deal_id}


def deal_filters_from_query(query: str) -> dict[str, str]:
    params = parse_qs(query)
    return {
        "deal_type": params.get("deal_type", [""])[0],
        "source_type": params.get("source_type", [""])[0],
        "partner_query": params.get("partner_query", [""])[0].strip(),
        "date_from": params.get("date_from", [""])[0],
        "date_to": params.get("date_to", [""])[0],
    }


def list_deals(conn: sqlite3.Connection, filters: dict[str, str] | None = None) -> list[dict[str, Any]]:
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


def get_deal(conn: sqlite3.Connection, deal_id: int) -> dict[str, Any] | None:
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


def get_summary(conn: sqlite3.Connection) -> dict[str, Any]:
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


def get_monthly_trends(conn: sqlite3.Connection) -> list[dict[str, Any]]:
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


def render_page(title: str, body: str) -> bytes:
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
</head>
<body>
  <header>
    <div class="wrap topbar">
      <h1>疑似freee会計ダッシュボード</h1>
      <nav><a href="/">取引一覧</a> <span class="label">/ API: POST /api/deals</span></nav>
    </div>
  </header>
  <main class="wrap">{body}</main>
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
          <form class="expense-form" method="post" action="/manual-expenses">
            <div class="form-grid">
              <label>発生日<input type="date" name="issue_date" value="{datetime.now().strftime("%Y-%m-%d")}" required></label>
              <label>支払予定日<input type="date" name="due_date" value="{datetime.now().strftime("%Y-%m-%d")}"></label>
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
            </div>
            <label>摘要<input name="description" placeholder="例: 梱包資材"></label>
            <label>メモ<textarea name="memo"></textarea></label>
            <div class="label">新しい取引先・勘定科目・税区分は下のマスタ設定で追加してください。</div>
            <button type="submit">経費を登録</button>
          </form>
        </div>
        <div>
          <div class="toolbar"><h2>月次推移</h2></div>
          <table>
            <thead><tr><th>月</th><th class="num">売上</th><th class="num">仕入</th><th class="num">経費</th><th class="num">粗利</th><th class="num">件数</th></tr></thead>
            <tbody>{trend_rows or '<tr><td colspan="6">まだ月次データはありません。</td></tr>'}</tbody>
          </table>
        </div>
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
