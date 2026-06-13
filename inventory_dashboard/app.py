from __future__ import annotations

import json
import math
import os
import sqlite3
import urllib.error
import urllib.request
from calendar import monthrange
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "inventory.db"
HOST = "127.0.0.1"
PORT = 8000
DEMO_HISTORY_MONTHS = 24
PSEUDO_FREEE_API_URL = os.environ.get("PSEUDO_FREEE_API_URL", "http://127.0.0.1:8010").rstrip("/")


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT NOT NULL UNIQUE,
    product_name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    supplier_name TEXT NOT NULL DEFAULT '',
    purchase_unit_price REAL NOT NULL DEFAULT 0,
    sales_unit_price REAL NOT NULL DEFAULT 0,
    tax_rate REAL NOT NULL DEFAULT 10,
    tax_category TEXT NOT NULL DEFAULT '課税仕入/課税売上 10%',
    lead_time_days INTEGER NOT NULL DEFAULT 7,
    safety_stock INTEGER NOT NULL DEFAULT 0,
    reorder_point INTEGER NOT NULL DEFAULT 0,
    min_order_quantity INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS business_partners (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    partner_type TEXT NOT NULL CHECK (partner_type IN ('supplier', 'customer')),
    partner_name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(partner_type, partner_name)
);

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    partner_name TEXT NOT NULL,
    invoice_no TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    received_date TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    tax_rate REAL NOT NULL DEFAULT 10,
    tax_category TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL DEFAULT '',
    external_accounting_status TEXT NOT NULL DEFAULT 'pending',
    external_accounting_id TEXT NOT NULL DEFAULT '',
    sync_error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sales (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    partner_name TEXT NOT NULL,
    invoice_no TEXT NOT NULL,
    transaction_date TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit_price REAL NOT NULL CHECK (unit_price >= 0),
    tax_rate REAL NOT NULL DEFAULT 10,
    tax_category TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL DEFAULT '',
    external_accounting_status TEXT NOT NULL DEFAULT 'pending',
    external_accounting_id TEXT NOT NULL DEFAULT '',
    sync_error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS inventory_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    movement_type TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    movement_date TEXT NOT NULL,
    quantity_delta INTEGER NOT NULL,
    unit_price REAL NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS freee_sync_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id INTEGER NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    payload_json TEXT NOT NULL,
    external_accounting_id TEXT NOT NULL DEFAULT '',
    sync_error_message TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_type, source_id)
);

CREATE TABLE IF NOT EXISTS inventory_corrections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    original_movement_id INTEGER NOT NULL REFERENCES inventory_movements(id),
    correction_movement_id INTEGER NOT NULL REFERENCES inventory_movements(id),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(original_movement_id)
);
"""


def dict_factory(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict[str, Any]:
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


@contextmanager
def get_conn() -> Any:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = dict_factory
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(SCHEMA_SQL)
        count = conn.execute("SELECT COUNT(*) AS count FROM products").fetchone()["count"]
        if count == 0:
            seed_products(conn)
        normalize_initial_stock_dates(conn)
        ensure_sample_transactions(conn)
        ensure_demo_history(conn)
        sync_partner_master(conn)


def seed_products(conn: sqlite3.Connection) -> None:
    products = [
        ("SKU-USB-C-001", "USB-Cケーブル 1m", "ケーブル", "東京サプライ", 480, 980, 10, 5, 20, 30, 10),
        ("SKU-MOUSE-001", "ワイヤレスマウス", "周辺機器", "関東OA商事", 1200, 2480, 10, 7, 10, 18, 5),
        ("SKU-MONITOR-024", "24インチモニター", "PC関連", "関東OA商事", 13500, 19800, 10, 14, 4, 8, 2),
    ]
    conn.executemany(
        """
        INSERT INTO products (
            sku, product_name, category, supplier_name, purchase_unit_price,
            sales_unit_price, tax_rate, lead_time_days, safety_stock,
            reorder_point, min_order_quantity
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        products,
    )
    rows = conn.execute("SELECT id, product_name, purchase_unit_price FROM products").fetchall()
    initial_stock = {"USB-Cケーブル 1m": 25, "ワイヤレスマウス": 12, "24インチモニター": 6}
    for row in rows:
        conn.execute(
            """
            INSERT INTO inventory_movements (
                product_id, movement_type, source_type, source_id, movement_date,
                quantity_delta, unit_price, note
            )
            VALUES (?, 'initial_stock', 'seed', ?, ?, ?, ?, '初期在庫')
            """,
            (row["id"], row["id"], initial_stock_date(), initial_stock[row["product_name"]], row["purchase_unit_price"]),
        )


def initial_stock_date() -> str:
    return add_months(date.today().replace(day=1), -DEMO_HISTORY_MONTHS).isoformat()


def normalize_initial_stock_dates(conn: sqlite3.Connection) -> None:
    stock_date = initial_stock_date()
    conn.execute(
        """
        UPDATE inventory_movements
        SET movement_date = ?
        WHERE movement_type = 'initial_stock'
          AND source_type = 'seed'
          AND movement_date <> ?
        """,
        (stock_date, stock_date),
    )


def ensure_sample_transactions(conn: sqlite3.Connection) -> None:
    purchase_count = conn.execute("SELECT COUNT(*) AS count FROM purchases").fetchone()["count"]
    sale_count = conn.execute("SELECT COUNT(*) AS count FROM sales").fetchone()["count"]
    if purchase_count or sale_count:
        return

    products = {row["sku"]: row for row in conn.execute("SELECT * FROM products").fetchall()}
    samples = [
        ("purchase", "SKU-USB-C-001", "東京サプライ", "P-202606-001", "2026-06-01", "2026-06-03", 40, 480, "課税仕入 10%", "2026-06-30"),
        ("sale", "SKU-USB-C-001", "青山ECストア", "S-202606-014", "2026-06-05", "", 22, 980, "課税売上 10%", "2026-07-31"),
        ("purchase", "SKU-MOUSE-001", "関東OA商事", "P-202606-008", "2026-06-02", "2026-06-06", 20, 1200, "課税仕入 10%", "2026-06-30"),
        ("sale", "SKU-MOUSE-001", "新宿デザイン事務所", "S-202606-021", "2026-06-08", "", 14, 2480, "課税売上 10%", "2026-07-31"),
        ("purchase", "SKU-MONITOR-024", "関東OA商事", "P-202606-011", "2026-06-04", "2026-06-11", 5, 13500, "課税仕入 10%", "2026-07-31"),
        ("sale", "SKU-MONITOR-024", "日本橋システムズ", "S-202606-033", "2026-06-12", "", 3, 19800, "課税売上 10%", "2026-07-31"),
    ]
    for kind, sku, partner_name, invoice_no, transaction_date, received_date, quantity, unit_price, tax_category, due_date in samples:
        product = products[sku]
        data = {
            "product_id": product["id"],
            "partner_name": partner_name,
            "invoice_no": invoice_no,
            "transaction_date": transaction_date,
            "quantity": quantity,
            "unit_price": unit_price,
            "tax_rate": product["tax_rate"],
            "tax_category": tax_category,
            "due_date": due_date,
        }
        if kind == "purchase":
            data["received_date"] = received_date
            create_purchase(conn, data)
        else:
            create_sale(conn, data)


def ensure_demo_history(conn: sqlite3.Connection) -> None:
    exists = conn.execute(
        "SELECT id FROM sales WHERE invoice_no LIKE 'DEMO-HIST-S-%' LIMIT 1"
    ).fetchone()
    if exists:
        return

    products = {row["sku"]: row for row in conn.execute("SELECT * FROM products").fetchall()}
    today = date.today()
    first_month = add_months(today.replace(day=1), -DEMO_HISTORY_MONTHS)
    patterns = {
        "SKU-USB-C-001": {"base": 36, "partner": "青山ECストア", "season": 1.2},
        "SKU-MOUSE-001": {"base": 18, "partner": "新宿デザイン事務所", "season": 1.1},
        "SKU-MONITOR-024": {"base": 5, "partner": "日本橋システムズ", "season": 1.35},
    }

    for month_index in range(DEMO_HISTORY_MONTHS):
        month_date = add_months(first_month, month_index)
        if month_date.year == today.year and month_date.month == today.month:
            continue
        for sku, pattern in patterns.items():
            product = products[sku]
            quantity = demo_monthly_sales_quantity(pattern["base"], pattern["season"], month_date, month_index)
            purchase_date = safe_date(month_date.year, month_date.month, 3)
            sale_date_a = safe_date(month_date.year, month_date.month, 12)
            sale_date_b = safe_date(month_date.year, month_date.month, 24)
            first_sale_quantity = max(quantity // 2, 1)
            second_sale_quantity = quantity - first_sale_quantity

            insert_demo_purchase(conn, product, purchase_date, quantity)
            insert_demo_sale(conn, product, pattern["partner"], sale_date_a, first_sale_quantity, "A")
            if second_sale_quantity > 0:
                insert_demo_sale(conn, product, pattern["partner"], sale_date_b, second_sale_quantity, "B")


def add_months(value: date, months: int) -> date:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def safe_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, monthrange(year, month)[1]))


def demo_monthly_sales_quantity(base: int, season_strength: float, month_date: date, month_index: int) -> int:
    seasonal_months = {3, 6, 11, 12}
    season = season_strength if month_date.month in seasonal_months else 1.0
    trend = 1 + (month_index / max(DEMO_HISTORY_MONTHS - 1, 1)) * 0.12
    wave = 1 + (((month_index % 5) - 2) * 0.04)
    return max(int(round(base * season * trend * wave)), 1)


def insert_demo_purchase(conn: sqlite3.Connection, product: dict[str, Any], purchase_date: date, quantity: int) -> None:
    invoice_no = f"DEMO-HIST-P-{product['sku']}-{purchase_date:%Y%m}"
    created_at = purchase_date.isoformat()
    cursor = conn.execute(
        """
        INSERT INTO purchases (
            product_id, partner_name, invoice_no, transaction_date, received_date,
            quantity, unit_price, tax_rate, tax_category, due_date,
            external_accounting_status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'demo', ?)
        """,
        (
            product["id"],
            product["supplier_name"],
            invoice_no,
            created_at,
            created_at,
            quantity,
            product["purchase_unit_price"],
            product["tax_rate"],
            "課税仕入 10%",
            safe_date(purchase_date.year, purchase_date.month, 28).isoformat(),
            created_at,
        ),
    )
    purchase_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO inventory_movements (
            product_id, movement_type, source_type, source_id, movement_date,
            quantity_delta, unit_price, note, created_at
        )
        VALUES (?, 'purchase_receipt', 'purchase', ?, ?, ?, ?, ?, ?)
        """,
        (product["id"], purchase_id, created_at, quantity, product["purchase_unit_price"], f"デモ仕入 {invoice_no}", created_at),
    )


def insert_demo_sale(
    conn: sqlite3.Connection,
    product: dict[str, Any],
    partner_name: str,
    sale_date: date,
    quantity: int,
    suffix: str,
) -> None:
    invoice_no = f"DEMO-HIST-S-{product['sku']}-{sale_date:%Y%m}-{suffix}"
    created_at = sale_date.isoformat()
    cursor = conn.execute(
        """
        INSERT INTO sales (
            product_id, partner_name, invoice_no, transaction_date,
            quantity, unit_price, tax_rate, tax_category, due_date,
            external_accounting_status, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'demo', ?)
        """,
        (
            product["id"],
            partner_name,
            invoice_no,
            created_at,
            quantity,
            product["sales_unit_price"],
            product["tax_rate"],
            "課税売上 10%",
            safe_date(sale_date.year, sale_date.month, 28).isoformat(),
            created_at,
        ),
    )
    sale_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO inventory_movements (
            product_id, movement_type, source_type, source_id, movement_date,
            quantity_delta, unit_price, note, created_at
        )
        VALUES (?, 'sale_shipment', 'sale', ?, ?, ?, ?, ?, ?)
        """,
        (product["id"], sale_id, created_at, -quantity, product["sales_unit_price"], f"デモ売上 {invoice_no}", created_at),
    )


def stock_by_product(conn: sqlite3.Connection) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT product_id, COALESCE(SUM(quantity_delta), 0) AS stock_quantity
        FROM inventory_movements
        GROUP BY product_id
        """
    ).fetchall()
    return {row["product_id"]: int(row["stock_quantity"]) for row in rows}


def list_products(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    stocks = stock_by_product(conn)
    products = conn.execute("SELECT * FROM products ORDER BY id").fetchall()
    for product in products:
        stock = stocks.get(product["id"], 0)
        product["stock_quantity"] = stock
        product["stock_value"] = stock * product["purchase_unit_price"]
        product["status"] = stock_status(product)
        product["required_stock_level"] = int(product["reorder_point"]) + int(product["safety_stock"])
        product["recommended_order_quantity"] = recommended_order_quantity(product, stock)
    return products


def sync_partner_master(conn: sqlite3.Connection) -> None:
    supplier_names = [
        row["partner_name"]
        for row in conn.execute(
            """
            SELECT supplier_name AS partner_name FROM products WHERE supplier_name <> ''
            UNION
            SELECT partner_name FROM purchases WHERE partner_name <> ''
            """
        ).fetchall()
    ]
    customer_names = [
        row["partner_name"]
        for row in conn.execute("SELECT DISTINCT partner_name FROM sales WHERE partner_name <> ''").fetchall()
    ]
    for name in supplier_names:
        add_business_partner(conn, "supplier", name)
    for name in customer_names:
        add_business_partner(conn, "customer", name)


def add_business_partner(conn: sqlite3.Connection, partner_type: str, partner_name: str) -> None:
    if partner_type not in {"supplier", "customer"}:
        raise ValueError("invalid partner_type")
    name = required_text(partner_name, "partner_name")
    conn.execute(
        """
        INSERT OR IGNORE INTO business_partners (partner_type, partner_name)
        VALUES (?, ?)
        """,
        (partner_type, name),
    )


def list_business_partners(conn: sqlite3.Connection) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT partner_type, partner_name
        FROM business_partners
        ORDER BY partner_type, partner_name
        """
    ).fetchall()
    partners = {"suppliers": [], "customers": []}
    for row in rows:
        key = "suppliers" if row["partner_type"] == "supplier" else "customers"
        partners[key].append(row["partner_name"])
    return partners


def create_business_partner(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    partner_type = data.get("partner_type", "")
    partner_name = required_text(data.get("partner_name"), "partner_name")
    add_business_partner(conn, partner_type, partner_name)
    return {"ok": True, "partner_type": partner_type, "partner_name": partner_name}


def stock_status(product: dict[str, Any]) -> str:
    stock = int(product["stock_quantity"])
    required_stock_level = int(product["reorder_point"]) + int(product["safety_stock"])
    if stock <= 0:
        return "欠品"
    if stock < required_stock_level:
        return "必要水準割れ"
    return "正常"


def recommended_order_quantity(product: dict[str, Any], stock: int) -> int:
    return max(int(product["reorder_point"]) + int(product["safety_stock"]) - stock, 0)


def create_product(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    required = ["sku", "product_name"]
    for key in required:
        if not str(data.get(key, "")).strip():
            raise ValueError(f"{key} is required")
    supplier_name = data.get("supplier_name", "").strip()
    conn.execute(
        """
        INSERT INTO products (
            sku, product_name, category, supplier_name, purchase_unit_price,
            sales_unit_price, tax_rate, tax_category, lead_time_days,
            safety_stock, reorder_point, min_order_quantity
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            data["sku"].strip(),
            data["product_name"].strip(),
            data.get("category", "").strip(),
            supplier_name,
            to_float(data.get("purchase_unit_price", 0)),
            to_float(data.get("sales_unit_price", 0)),
            to_float(data.get("tax_rate", 10)),
            data.get("tax_category", "課税仕入/課税売上 10%").strip(),
            to_int(data.get("lead_time_days", 7)),
            to_int(data.get("safety_stock", 0)),
            to_int(data.get("reorder_point", 0)),
            max(to_int(data.get("min_order_quantity", 1)), 1),
        ),
    )
    if supplier_name:
        add_business_partner(conn, "supplier", supplier_name)
    return {"ok": True}


def create_purchase(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    product = get_product(conn, to_int(data.get("product_id")))
    quantity = positive_int(data.get("quantity"), "quantity")
    unit_price = to_float(data.get("unit_price", product["purchase_unit_price"]))
    transaction_date = data.get("transaction_date") or date.today().isoformat()
    received_date = data.get("received_date") or transaction_date
    tax_rate = to_float(data.get("tax_rate", product["tax_rate"]))
    tax_category = data.get("tax_category") or product["tax_category"]
    partner_name = required_text(data.get("partner_name") or product["supplier_name"], "partner_name")
    invoice_no = required_text(data.get("invoice_no"), "invoice_no")
    due_date = data.get("due_date") or ""

    cursor = conn.execute(
        """
        INSERT INTO purchases (
            product_id, partner_name, invoice_no, transaction_date, received_date,
            quantity, unit_price, tax_rate, tax_category, due_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (product["id"], partner_name, invoice_no, transaction_date, received_date, quantity, unit_price, tax_rate, tax_category, due_date),
    )
    purchase_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO inventory_movements (
            product_id, movement_type, source_type, source_id, movement_date,
            quantity_delta, unit_price, note
        )
        VALUES (?, 'purchase_receipt', 'purchase', ?, ?, ?, ?, ?)
        """,
        (product["id"], purchase_id, received_date, quantity, unit_price, f"仕入 {invoice_no}"),
    )
    enqueue_freee_payload(conn, "purchase", purchase_id)
    add_business_partner(conn, "supplier", partner_name)
    return {"ok": True, "purchase_id": purchase_id}


def create_sale(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    product = get_product(conn, to_int(data.get("product_id")))
    quantity = positive_int(data.get("quantity"), "quantity")
    current_stock = stock_by_product(conn).get(product["id"], 0)
    if current_stock < quantity:
        raise ValueError(f"在庫不足です。現在庫 {current_stock} に対して出庫数量 {quantity} は登録できません。")
    unit_price = to_float(data.get("unit_price", product["sales_unit_price"]))
    transaction_date = data.get("transaction_date") or date.today().isoformat()
    tax_rate = to_float(data.get("tax_rate", product["tax_rate"]))
    tax_category = data.get("tax_category") or product["tax_category"]
    partner_name = required_text(data.get("partner_name"), "partner_name")
    invoice_no = required_text(data.get("invoice_no"), "invoice_no")
    due_date = data.get("due_date") or ""

    cursor = conn.execute(
        """
        INSERT INTO sales (
            product_id, partner_name, invoice_no, transaction_date,
            quantity, unit_price, tax_rate, tax_category, due_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (product["id"], partner_name, invoice_no, transaction_date, quantity, unit_price, tax_rate, tax_category, due_date),
    )
    sale_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO inventory_movements (
            product_id, movement_type, source_type, source_id, movement_date,
            quantity_delta, unit_price, note
        )
        VALUES (?, 'sale_shipment', 'sale', ?, ?, ?, ?, ?)
        """,
        (product["id"], sale_id, transaction_date, -quantity, unit_price, f"売上 {invoice_no}"),
    )
    enqueue_freee_payload(conn, "sale", sale_id)
    add_business_partner(conn, "customer", partner_name)
    return {"ok": True, "sale_id": sale_id}


def enqueue_freee_payload(conn: sqlite3.Connection, source_type: str, source_id: int) -> None:
    payload = build_freee_payload(conn, source_type, source_id)
    direction = "expense" if source_type == "purchase" else "income"
    conn.execute(
        """
        INSERT INTO freee_sync_queue (source_type, source_id, direction, status, payload_json)
        VALUES (?, ?, ?, 'pending', ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (source_type, source_id, direction, json.dumps(payload, ensure_ascii=False)),
    )


def build_freee_payload(conn: sqlite3.Connection, source_type: str, source_id: int) -> dict[str, Any]:
    if source_type == "purchase":
        row = conn.execute(
            """
            SELECT p.*, pr.sku, pr.product_name
            FROM purchases p
            JOIN products pr ON pr.id = p.product_id
            WHERE p.id = ?
            """,
            (source_id,),
        ).fetchone()
        if not row:
            raise ValueError("purchase not found")
        amount = round(row["quantity"] * row["unit_price"] * (1 + row["tax_rate"] / 100))
        return {
            "api_target": "freee_accounting_deal",
            "issue_date": row["transaction_date"],
            "due_date": row["due_date"],
            "type": "expense",
            "partner_name": row["partner_name"],
            "invoice_no": row["invoice_no"],
            "details": [
                {
                    "sku": row["sku"],
                    "description": row["product_name"],
                    "quantity": row["quantity"],
                    "unit_price": row["unit_price"],
                    "tax_rate": row["tax_rate"],
                    "tax_category": row["tax_category"],
                    "amount": amount,
                    "account_item_name": "仕入高",
                }
            ],
        }
    if source_type == "sale":
        row = conn.execute(
            """
            SELECT s.*, pr.sku, pr.product_name
            FROM sales s
            JOIN products pr ON pr.id = s.product_id
            WHERE s.id = ?
            """,
            (source_id,),
        ).fetchone()
        if not row:
            raise ValueError("sale not found")
        amount = round(row["quantity"] * row["unit_price"] * (1 + row["tax_rate"] / 100))
        return {
            "api_target": "freee_accounting_deal",
            "issue_date": row["transaction_date"],
            "due_date": row["due_date"],
            "type": "income",
            "partner_name": row["partner_name"],
            "invoice_no": row["invoice_no"],
            "details": [
                {
                    "sku": row["sku"],
                    "description": row["product_name"],
                    "quantity": row["quantity"],
                    "unit_price": row["unit_price"],
                    "tax_rate": row["tax_rate"],
                    "tax_category": row["tax_category"],
                    "amount": amount,
                    "account_item_name": "売上高",
                }
            ],
        }
    raise ValueError("invalid source_type")


def get_product(conn: sqlite3.Connection, product_id: int) -> dict[str, Any]:
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        raise ValueError("product not found")
    return product


def dashboard(conn: sqlite3.Connection) -> dict[str, Any]:
    products = list_products(conn)
    total_stock_value = sum(product["stock_value"] for product in products)
    recent_movements = conn.execute(
        """
        SELECT im.*, p.sku, p.product_name
        FROM inventory_movements im
        JOIN products p ON p.id = im.product_id
        ORDER BY im.created_at DESC, im.id DESC
        LIMIT 10
        """
    ).fetchall()
    today = date.today()
    month_start = today.replace(day=1).isoformat()
    today_text = today.isoformat()
    monthly_purchases = conn.execute(
        """
        SELECT
            p.sku,
            p.product_name,
            SUM(pu.quantity) AS quantity,
            SUM(pu.quantity * pu.unit_price) AS amount
        FROM purchases pu
        JOIN products p ON p.id = pu.product_id
        WHERE pu.transaction_date BETWEEN ? AND ?
        GROUP BY p.id, p.sku, p.product_name
        ORDER BY p.id
        """,
        (month_start, today_text),
    ).fetchall()
    monthly_sales = conn.execute(
        """
        SELECT
            p.sku,
            p.product_name,
            SUM(s.quantity) AS quantity,
            SUM(s.quantity * s.unit_price) AS amount
        FROM sales s
        JOIN products p ON p.id = s.product_id
        WHERE s.transaction_date BETWEEN ? AND ?
        GROUP BY p.id, p.sku, p.product_name
        ORDER BY p.id
        """,
        (month_start, today_text),
    ).fetchall()
    monthly_purchase_total = conn.execute(
        """
        SELECT COALESCE(SUM(quantity * unit_price), 0) AS total
        FROM purchases
        WHERE transaction_date BETWEEN ? AND ?
        """,
        (month_start, today_text),
    ).fetchone()["total"]
    monthly_sales_total = conn.execute(
        """
        SELECT COALESCE(SUM(quantity * unit_price), 0) AS total
        FROM sales
        WHERE transaction_date BETWEEN ? AND ?
        """,
        (month_start, today_text),
    ).fetchone()["total"]
    forecast = forecast_simulation(conn, 30)
    forecast_by_sku = {row["sku"]: row for row in forecast["rows"]}
    for product in products:
        forecast_row = forecast_by_sku.get(product["sku"])
        if not forecast_row:
            continue
        product["required_stock_level"] = forecast_row["required_inventory"]
        product["recommended_order_quantity"] = forecast_row["recommended_order_quantity"]
        product["lead_time_demand"] = forecast_row["lead_time_demand"]
        product["forecast_basis"] = "直近30日予測"
        if int(product["stock_quantity"]) <= 0:
            product["status"] = "欠品"
        elif int(product["recommended_order_quantity"]) > 0:
            product["status"] = "必要水準割れ"
        else:
            product["status"] = "正常"
    return {
        "total_stock_value": total_stock_value,
        "product_count": len(products),
        "reorder_count": len([p for p in products if p["status"] in ("欠品", "必要水準割れ", "発注点割れ")]),
        "month_start": month_start,
        "month_end": today_text,
        "monthly_purchase_total": monthly_purchase_total,
        "monthly_sales_total": monthly_sales_total,
        "monthly_purchases": monthly_purchases,
        "monthly_sales": monthly_sales,
        "products": products,
        "recent_movements": recent_movements,
    }


def forecast_simulation(conn: sqlite3.Connection, horizon_days: int = 30) -> dict[str, Any]:
    if horizon_days not in {30, 60, 90}:
        horizon_days = 30

    today = date.today()
    start_date = (today - timedelta(days=horizon_days - 1)).isoformat()
    end_date = today.isoformat()
    month_end = date(today.year, today.month, monthrange(today.year, today.month)[1])
    days_to_month_end = max((month_end - today).days + 1, 0)
    products = list_products(conn)
    rows = []

    for product in products:
        recent_sales_quantity = active_sales_quantity(conn, product["id"], start_date, end_date)
        total_sales_quantity = active_sales_quantity(conn, product["id"], "1900-01-01", end_date)
        daily_average = recent_sales_quantity / horizon_days
        seasonal_factor = monthly_seasonal_factor(conn, product["id"], today.month)
        adjusted_daily_average = daily_average * seasonal_factor
        month_end_forecast = math.ceil(adjusted_daily_average * days_to_month_end)
        lead_time_demand = math.ceil(adjusted_daily_average * int(product["lead_time_days"]))
        required_inventory = lead_time_demand + int(product["safety_stock"])
        recommended_order_quantity = max(required_inventory - int(product["stock_quantity"]), 0)
        projected_month_end_stock = int(product["stock_quantity"]) - month_end_forecast
        projected_month_end_stock_after_order = int(product["stock_quantity"]) + recommended_order_quantity - month_end_forecast
        month_end_shortage = abs(min(projected_month_end_stock_after_order, 0))

        if total_sales_quantity == 0:
            lead_time_judgement = "データ不足"
        elif recommended_order_quantity > 0:
            lead_time_judgement = "発注推奨"
        else:
            lead_time_judgement = "発注不要"

        if total_sales_quantity == 0:
            month_end_judgement = "データ不足"
        elif month_end_shortage > 0:
            month_end_judgement = "月末不足"
        else:
            month_end_judgement = "月末OK"

        rows.append(
            {
                "sku": product["sku"],
                "product_name": product["product_name"],
                "stock_quantity": product["stock_quantity"],
                "recent_sales_quantity": recent_sales_quantity,
                "daily_average": round(daily_average, 2),
                "seasonal_factor": round(seasonal_factor, 2),
                "month_end_forecast": month_end_forecast,
                "lead_time_days": product["lead_time_days"],
                "lead_time_demand": lead_time_demand,
                "safety_stock": product["safety_stock"],
                "required_inventory": required_inventory,
                "recommended_order_quantity": recommended_order_quantity,
                "projected_month_end_stock": projected_month_end_stock,
                "projected_month_end_stock_after_order": projected_month_end_stock_after_order,
                "month_end_shortage": month_end_shortage,
                "lead_time_judgement": lead_time_judgement,
                "month_end_judgement": month_end_judgement,
                "judgement": lead_time_judgement,
            }
        )

    return {
        "horizon_days": horizon_days,
        "start_date": start_date,
        "end_date": end_date,
        "month_end": month_end.isoformat(),
        "days_to_month_end": days_to_month_end,
        "rows": rows,
    }


def active_sales_quantity(conn: sqlite3.Connection, product_id: int, start_date: str, end_date: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(s.quantity), 0) AS quantity
        FROM sales s
        JOIN inventory_movements im ON im.source_type = 'sale' AND im.source_id = s.id
        LEFT JOIN inventory_corrections c ON c.original_movement_id = im.id
        WHERE s.product_id = ?
          AND s.transaction_date BETWEEN ? AND ?
          AND c.id IS NULL
        """,
        (product_id, start_date, end_date),
    ).fetchone()
    return int(row["quantity"] or 0)


def monthly_seasonal_factor(conn: sqlite3.Connection, product_id: int, target_month: int) -> float:
    rows = conn.execute(
        """
        SELECT CAST(strftime('%m', s.transaction_date) AS INTEGER) AS month,
               SUM(s.quantity) AS quantity
        FROM sales s
        JOIN inventory_movements im ON im.source_type = 'sale' AND im.source_id = s.id
        LEFT JOIN inventory_corrections c ON c.original_movement_id = im.id
        WHERE s.product_id = ?
          AND c.id IS NULL
        GROUP BY CAST(strftime('%m', s.transaction_date) AS INTEGER)
        """,
        (product_id,),
    ).fetchall()
    if len(rows) < 6:
        return 1.0
    quantities = [float(row["quantity"] or 0) for row in rows]
    average = sum(quantities) / len(quantities)
    if average <= 0:
        return 1.0
    target = next((float(row["quantity"] or 0) for row in rows if int(row["month"]) == target_month), average)
    return min(max(target / average, 0.75), 1.4)


def product_ledger(conn: sqlite3.Connection, product_id: int) -> dict[str, Any]:
    product = get_product(conn, product_id)
    rows = conn.execute(
        """
        SELECT
            im.id,
            im.movement_date,
            im.movement_type,
            im.source_type,
            im.source_id,
            im.quantity_delta,
            im.unit_price,
            im.note,
            COALESCE(pu.partner_name, sa.partner_name, '') AS partner_name,
            COALESCE(pu.invoice_no, sa.invoice_no, '') AS invoice_no,
            COALESCE(pu.tax_category, sa.tax_category, '') AS tax_category,
            COALESCE(pu.external_accounting_status, sa.external_accounting_status, '') AS accounting_status,
            COALESCE(q.status, '') AS queue_status,
            COALESCE(q.sync_error_message, '') AS sync_error_message,
            CASE WHEN c.original_movement_id IS NULL THEN 0 ELSE 1 END AS is_cancelled,
            CASE WHEN im.source_type = 'correction' THEN 1 ELSE 0 END AS is_correction
        FROM inventory_movements im
        LEFT JOIN purchases pu ON im.source_type = 'purchase' AND pu.id = im.source_id
        LEFT JOIN sales sa ON im.source_type = 'sale' AND sa.id = im.source_id
        LEFT JOIN freee_sync_queue q ON q.source_type = im.source_type AND q.source_id = im.source_id
        LEFT JOIN inventory_corrections c ON c.original_movement_id = im.id
        WHERE im.product_id = ?
        ORDER BY im.movement_date ASC, im.id ASC
        """,
        (product_id,),
    ).fetchall()

    balance = 0
    valuation_unit_price = float(product["purchase_unit_price"])
    for row in rows:
        quantity_delta = int(row["quantity_delta"])
        balance += quantity_delta
        row["in_quantity"] = max(quantity_delta, 0)
        row["out_quantity"] = abs(min(quantity_delta, 0))
        row["balance"] = balance
        row["amount"] = abs(quantity_delta) * float(row["unit_price"])
        row["inventory_balance_amount"] = balance * valuation_unit_price
    product["stock_quantity"] = balance
    product["inventory_balance_amount"] = balance * valuation_unit_price
    product["valuation_unit_price"] = valuation_unit_price
    rows.reverse()
    return {"product": product, "ledger": rows, "count": len(rows)}


def cancel_inventory_movement(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    movement_id = to_int(data.get("movement_id"))
    reason = required_text(data.get("reason") or "入力ミスのため取消", "reason")
    original = conn.execute("SELECT * FROM inventory_movements WHERE id = ?", (movement_id,)).fetchone()
    if not original:
        raise ValueError("movement not found")
    if original["source_type"] not in {"purchase", "sale"}:
        raise ValueError("仕入入庫または売上出庫のみ取消できます。")
    exists = conn.execute(
        "SELECT id FROM inventory_corrections WHERE original_movement_id = ?",
        (movement_id,),
    ).fetchone()
    if exists:
        raise ValueError("この元帳行はすでに取消済みです。")

    reversal_delta = -int(original["quantity_delta"])
    if reversal_delta < 0:
        current_stock = stock_by_product(conn).get(original["product_id"], 0)
        if current_stock + reversal_delta < 0:
            raise ValueError("取消すると在庫がマイナスになるため、先に関連する売上や調整を確認してください。")

    movement_type = "purchase_cancel" if original["source_type"] == "purchase" else "sale_cancel"
    cursor = conn.execute(
        """
        INSERT INTO inventory_movements (
            product_id, movement_type, source_type, source_id, movement_date,
            quantity_delta, unit_price, note
        )
        VALUES (?, ?, 'correction', ?, ?, ?, ?, ?)
        """,
        (
            original["product_id"],
            movement_type,
            movement_id,
            date.today().isoformat(),
            reversal_delta,
            original["unit_price"],
            f"取消: {original['note']} / 理由: {reason}",
        ),
    )
    correction_movement_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO inventory_corrections (original_movement_id, correction_movement_id, reason)
        VALUES (?, ?, ?)
        """,
        (movement_id, correction_movement_id, reason),
    )
    conn.execute(
        """
        UPDATE freee_sync_queue
        SET status = 'failed',
            sync_error_message = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE source_type = ? AND source_id = ? AND status IN ('pending', 'retry')
        """,
        (f"在庫元帳で取消済み: {reason}", original["source_type"], original["source_id"]),
    )
    return {"ok": True, "correction_movement_id": correction_movement_id}


def list_queue(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT q.*
        FROM freee_sync_queue q
        ORDER BY q.created_at DESC, q.id DESC
        """
    ).fetchall()
    for row in rows:
        row["payload"] = json.loads(row["payload_json"])
    return rows


def mark_queue_status(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    queue_id = to_int(data.get("id"))
    status = data.get("status", "")
    if status not in {"pending", "sent", "failed", "retry"}:
        raise ValueError("invalid status")
    conn.execute(
        """
        UPDATE freee_sync_queue
        SET status = ?, external_accounting_id = ?, sync_error_message = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (status, data.get("external_accounting_id", ""), data.get("sync_error_message", ""), queue_id),
    )
    return {"ok": True}


def fail_queue_send(conn: sqlite3.Connection, queue_id: int, message: str) -> None:
    conn.execute(
        """
        UPDATE freee_sync_queue
        SET status = 'failed',
            sync_error_message = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (message, queue_id),
    )


def send_queue_to_pseudo_freee(conn: sqlite3.Connection, data: dict[str, Any]) -> dict[str, Any]:
    queue_id = to_int(data.get("id"))
    queue = conn.execute("SELECT * FROM freee_sync_queue WHERE id = ?", (queue_id,)).fetchone()
    if not queue:
        raise ValueError("queue not found")
    if queue["status"] == "sent":
        raise ValueError("送信済みキューは再送できません")

    payload = json.loads(queue["payload_json"])
    request_body = json.dumps(
        {
            "queue_id": queue["id"],
            "source_type": queue["source_type"],
            "source_id": queue["source_id"],
            "payload": payload,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{PSEUDO_FREEE_API_URL}/api/deals",
        data=request_body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response_data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        message = f"疑似freee送信失敗 HTTP {exc.code}: {error_body}"
        fail_queue_send(conn, queue_id, message)
        raise ValueError(message) from exc
    except urllib.error.URLError as exc:
        message = f"疑似freeeに接続できません: {exc.reason}"
        fail_queue_send(conn, queue_id, message)
        raise ValueError(message) from exc
    except TimeoutError as exc:
        message = "疑似freee送信がタイムアウトしました"
        fail_queue_send(conn, queue_id, message)
        raise ValueError(message) from exc

    if not response_data.get("ok"):
        message = str(response_data.get("error") or "疑似freee送信に失敗しました")
        fail_queue_send(conn, queue_id, message)
        raise ValueError(message)

    pseudo_freee_deal_id = response_data.get("pseudo_freee_deal_id")
    external_accounting_id = f"pseudo-freee-{pseudo_freee_deal_id}"
    conn.execute(
        """
        UPDATE freee_sync_queue
        SET status = 'sent',
            external_accounting_id = ?,
            sync_error_message = '',
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (external_accounting_id, queue_id),
    )
    return {
        "ok": True,
        "pseudo_freee_deal_id": pseudo_freee_deal_id,
        "external_accounting_id": external_accounting_id,
        "duplicate": bool(response_data.get("duplicate")),
    }


def to_int(value: Any) -> int:
    return int(value or 0)


def positive_int(value: Any, name: str) -> int:
    number = to_int(value)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def to_float(value: Any) -> float:
    return float(value or 0)


def required_text(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} is required")
    return text


class InventoryHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                self.respond_html(INDEX_HTML)
            elif parsed.path == "/api/dashboard":
                with get_conn() as conn:
                    self.respond_json(dashboard(conn))
            elif parsed.path == "/api/products":
                with get_conn() as conn:
                    self.respond_json(list_products(conn))
            elif parsed.path == "/api/business-partners":
                with get_conn() as conn:
                    self.respond_json(list_business_partners(conn))
            elif parsed.path == "/api/forecast-simulation":
                params = parse_qs(parsed.query)
                horizon_days = int(params.get("horizon_days", [30])[0])
                with get_conn() as conn:
                    self.respond_json(forecast_simulation(conn, horizon_days))
            elif parsed.path.startswith("/api/products/") and parsed.path.endswith("/ledger"):
                product_id = int(parsed.path.split("/")[3])
                with get_conn() as conn:
                    self.respond_json(product_ledger(conn, product_id))
            elif parsed.path == "/api/freee-sync-queue":
                with get_conn() as conn:
                    self.respond_json(list_queue(conn))
            elif parsed.path == "/api/freee-preview":
                params = parse_qs(parsed.query)
                source_type = params.get("source_type", [""])[0]
                source_id = int(params.get("source_id", [0])[0])
                with get_conn() as conn:
                    self.respond_json(build_freee_payload(conn, source_type, source_id))
            else:
                self.respond_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.respond_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            data = self.read_json()
            with get_conn() as conn:
                if parsed.path == "/api/products":
                    result = create_product(conn, data)
                elif parsed.path == "/api/purchases":
                    result = create_purchase(conn, data)
                elif parsed.path == "/api/sales":
                    result = create_sale(conn, data)
                elif parsed.path == "/api/business-partners":
                    result = create_business_partner(conn, data)
                elif parsed.path == "/api/freee-sync-queue/send":
                    result = send_queue_to_pseudo_freee(conn, data)
                elif parsed.path == "/api/freee-sync-queue/status":
                    result = mark_queue_status(conn, data)
                elif parsed.path == "/api/inventory-movements/cancel":
                    result = cancel_inventory_movement(conn, data)
                else:
                    self.respond_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                    return
            self.respond_json(result, HTTPStatus.CREATED)
        except sqlite3.IntegrityError as exc:
            self.respond_json({"error": f"database integrity error: {exc}"}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.respond_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(body or "{}")

    def respond_json(self, data: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def respond_html(self, html: str) -> None:
        body = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{timestamp}] {self.address_string()} {format % args}")


INDEX_HTML = r"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>在庫管理ダッシュボード</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #20242a;
      --muted: #68717d;
      --line: #d8dde5;
      --accent: #256c64;
      --accent-2: #b64b35;
      --warn: #a96500;
      --danger: #b3261e;
      --ok: #227447;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    header { padding: 18px 24px; background: #24313d; color: white; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    h1 { margin: 0; font-size: 20px; letter-spacing: 0; }
    main { padding: 16px 20px 28px; max-width: 1280px; margin: 0 auto; }
    section { margin: 0 0 14px; }
    h2 { margin: 0 0 12px; font-size: 17px; }
    .metrics { display: grid; grid-template-columns: repeat(5, minmax(150px, 1fr)); gap: 12px; }
    .metric, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
    .metric.risk-alert { background: #fff3f1; border-color: #f3beb7; }
    .metric span { display: block; color: var(--muted); font-size: 12px; }
    .metric strong { display: block; margin-top: 6px; font-size: 22px; }
    .metric.risk-alert span, .metric.risk-alert strong { color: var(--danger); }
    .top-grid { display: grid; grid-template-columns: minmax(430px, 1.45fr) minmax(260px, 1fr) minmax(260px, 1fr); gap: 14px; align-items: start; }
    .ledger-entry-grid { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 16px; align-items: start; }
    .bottom-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(360px, .72fr); gap: 16px; align-items: start; }
    .entry-panel { position: sticky; top: 14px; }
    .form-tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 4px; margin: 8px 0 12px; background: #eef1f4; border: 1px solid var(--line); border-radius: 8px; }
    .form-tab { padding: 9px 10px; border-radius: 6px; background: transparent; color: var(--muted); }
    .form-tab.active { background: white; color: var(--accent); box-shadow: 0 1px 2px rgba(32, 36, 42, .08); }
    .transaction-form { display: none; }
    .transaction-form.active { display: block; }
    .transaction-form h2 { margin-top: 4px; font-size: 15px; }
    .transaction-form button[type="submit"] { width: 100%; margin-top: 12px; }
    .partner-add { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; margin-top: 8px; }
    .partner-add button { padding: 9px 12px; white-space: nowrap; }
    label { display: block; font-size: 12px; color: var(--muted); margin: 8px 0 4px; }
    input, select { width: 100%; padding: 9px 10px; border: 1px solid var(--line); border-radius: 6px; background: white; font: inherit; }
    button { border: 0; border-radius: 6px; padding: 10px 12px; background: var(--accent); color: white; font-weight: 700; cursor: pointer; }
    button.link { background: transparent; color: var(--accent); padding: 0; text-align: left; text-decoration: underline; }
    button.secondary { background: #4d5966; }
    button.warning { background: var(--warn); }
    table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
    th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; font-size: 13px; }
    th { background: #eef1f4; color: #38424d; }
    tr:last-child td { border-bottom: 0; }
    .status { display: inline-flex; align-items: center; min-height: 22px; padding: 3px 8px; border-radius: 999px; font-size: 12px; line-height: 1; white-space: nowrap; background: #e8eef1; }
    .status.ok { color: var(--ok); }
    .status.warn { color: var(--warn); }
    .status.danger { color: var(--danger); }
    #products th:nth-child(5), #products td:nth-child(5) { min-width: 108px; }
    .note { margin: -4px 0 10px; color: var(--muted); font-size: 13px; }
    .table-total { display: flex; justify-content: flex-end; gap: 18px; align-items: baseline; padding: 10px 12px; background: white; border: 1px solid var(--line); border-top: 0; border-radius: 0 0 8px 8px; font-size: 13px; }
    .table-total strong { font-size: 16px; }
    .match { color: var(--ok); font-weight: 700; }
    .mismatch { color: var(--danger); font-weight: 700; }
    .error { color: var(--danger); font-size: 12px; font-weight: 700; }
    .summary-stack { display: grid; gap: 14px; }
    .summary-box { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
    .summary-box h3 { margin: 0 0 8px; font-size: 15px; }
    .section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
    .section-head h2 { margin: 0; }
    .inline-control { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
    .inline-control select { width: auto; min-width: 110px; padding: 7px 9px; }
    pre { white-space: pre-wrap; word-break: break-word; background: #1f2933; color: #f7fafc; padding: 12px; border-radius: 8px; max-height: 360px; overflow: auto; }
    .message { min-height: 24px; color: var(--accent-2); font-weight: 700; }
    @media (max-width: 900px) {
      .metrics, .top-grid, .ledger-entry-grid, .bottom-grid { grid-template-columns: 1fr; }
      .entry-panel { position: static; }
      main { padding: 12px; }
      table { display: block; overflow-x: auto; }
    }
  </style>
</head>
<body>
  <header>
    <h1>在庫管理ダッシュボード</h1>
    <button class="secondary" onclick="loadAll()">更新</button>
  </header>
  <main>
    <section class="metrics" id="metrics"></section>
    <section class="top-grid">
      <div class="panel">
        <h2>在庫一覧</h2>
        <div id="products"></div>
      </div>
      <div class="summary-box">
        <h3>今月仕入 商品別</h3>
        <div id="monthlyPurchases"></div>
      </div>
      <div class="summary-box">
        <h3>今月売上 商品別</h3>
        <div id="monthlySales"></div>
      </div>
    </section>
    <section class="ledger-entry-grid">
      <div class="panel">
        <div class="section-head">
          <h2 id="ledgerTitle">在庫元帳</h2>
          <label class="inline-control">表示商品
            <select id="ledgerProductSelect" onchange="loadLedger(this.value)"></select>
          </label>
        </div>
        <p class="note" id="ledgerNote">在庫一覧の商品名をクリックすると、仕入・売上・初期在庫から現在庫に至る記録を表示します。</p>
        <div id="ledger"></div>
      </div>
      <aside class="panel entry-panel">
        <h2>登録</h2>
        <div class="message" id="message"></div>
        <div class="form-tabs" role="tablist" aria-label="登録種別">
          <button class="form-tab active" type="button" data-form="purchaseForm" onclick="showTransactionForm('purchaseForm')">仕入</button>
          <button class="form-tab" type="button" data-form="saleForm" onclick="showTransactionForm('saleForm')">売上</button>
        </div>
        <div>
          <form id="purchaseForm" class="transaction-form active">
            <h2>仕入明細</h2>
            <label>商品</label><select name="product_id"></select>
            <label>仕入先</label><select name="partner_name" data-partner-type="supplier" required></select>
            <div class="partner-add">
              <input id="newSupplierName" placeholder="新しい仕入先名">
              <button type="button" onclick="addPartner('supplier', 'newSupplierName', 'purchaseForm')">追加</button>
            </div>
            <label>請求書番号</label><input name="invoice_no" required>
            <label>仕入日</label><input type="date" name="transaction_date" required>
            <label>入庫日</label><input type="date" name="received_date" required>
            <label>数量</label><input type="number" name="quantity" min="1" required>
            <label>単価</label><input type="number" name="unit_price" min="0" required>
            <label>税率</label><input type="number" name="tax_rate" value="10">
            <label>税区分</label><input name="tax_category" value="課税仕入 10%">
            <label>支払予定日</label><input type="date" name="due_date">
            <button type="submit">仕入登録</button>
          </form>
          <form id="saleForm" class="transaction-form">
            <h2>売上明細</h2>
            <label>商品</label><select name="product_id"></select>
            <label>得意先</label><select name="partner_name" data-partner-type="customer" required></select>
            <div class="partner-add">
              <input id="newCustomerName" placeholder="新しい得意先名">
              <button type="button" onclick="addPartner('customer', 'newCustomerName', 'saleForm')">追加</button>
            </div>
            <label>請求書/注文番号</label><input name="invoice_no" required>
            <label>売上日</label><input type="date" name="transaction_date" required>
            <label>数量</label><input type="number" name="quantity" min="1" required>
            <label>単価</label><input type="number" name="unit_price" min="0" required>
            <label>税率</label><input type="number" name="tax_rate" value="10">
            <label>税区分</label><input name="tax_category" value="課税売上 10%">
            <label>入金予定日</label><input type="date" name="due_date">
            <button type="submit">売上登録</button>
          </form>
        </div>
      </aside>
    </section>
    <section class="panel">
      <div class="section-head">
        <h2>適正在庫シミュレーション</h2>
        <label class="inline-control">予測期間
          <select id="forecastHorizon" onchange="loadForecast()">
            <option value="30">直近30日</option>
            <option value="60">直近60日</option>
            <option value="90">直近90日</option>
          </select>
        </label>
      </div>
      <p class="note" id="forecastNote">過去売上から月末販売数、リードタイム需要、必要在庫、推奨発注量を計算します。</p>
      <div id="forecastSimulation"></div>
    </section>
    <section class="bottom-grid">
      <div class="summary-box">
        <h2>freee送信待ちキュー</h2>
        <div id="queue"></div>
      </div>
      <div class="summary-box">
        <h2>送信前レビュー</h2>
        <pre id="preview">キューの「確認」を押すと、freee送信用の中間データを表示します。</pre>
      </div>
    </section>
  </main>
  <script>
    const yen = new Intl.NumberFormat("ja-JP", { style: "currency", currency: "JPY", maximumFractionDigits: 0 });
    const today = new Date().toISOString().slice(0, 10);
    let currentLedgerProductId = null;
    let currentLedgerData = null;
    let ledgerExpanded = false;
    let currentPartners = { suppliers: [], customers: [] };
    for (const input of document.querySelectorAll('input[type="date"][required]')) input.value = today;

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "request failed");
      return data;
    }

    async function loadAll() {
      const data = await api("/api/dashboard");
      renderMetrics(data);
      renderProducts(data.products);
      renderMonthlySummary("monthlyPurchases", data.monthly_purchases, data.monthly_purchase_total, "今月仕入 合計");
      renderMonthlySummary("monthlySales", data.monthly_sales, data.monthly_sales_total, "今月売上 合計");
      await loadForecast();
      renderSelects(data.products);
      await loadPartners();
      if (currentLedgerProductId) {
        await loadLedger(currentLedgerProductId);
      } else if (data.products.length && !document.getElementById("ledger").innerHTML) {
        await loadLedger(data.products[0].id);
      }
      const queue = await api("/api/freee-sync-queue");
      renderQueue(queue);
    }

    function renderMetrics(data) {
      window.dashboardStockTotal = Number(data.total_stock_value || 0);
      document.getElementById("metrics").innerHTML = [
        ["在庫総額", yen.format(data.total_stock_value), ""],
        ["商品数", data.product_count, ""],
        ["発注/欠品リスク", data.reorder_count, Number(data.reorder_count || 0) > 0 ? "risk-alert" : ""],
        ["今月仕入", yen.format(data.monthly_purchase_total), ""],
        ["今月売上", yen.format(data.monthly_sales_total), ""]
      ].map(([label, value, cls]) => `<div class="metric ${cls}"><span>${label}</span><strong>${value}</strong></div>`).join("");
    }

    function renderProducts(products) {
      const listTotal = products.reduce((sum, p) => sum + Number(p.stock_value || 0), 0);
      const dashboardTotal = Number(window.dashboardStockTotal || 0);
      const diff = listTotal - dashboardTotal;
      const diffText = diff === 0 ? `<span class="match">在庫総額と一致</span>` : `<span class="mismatch">差額 ${yen.format(diff)}</span>`;
      document.getElementById("products").innerHTML = table(["SKU", "商品", "必要水準", "現在在庫", "状態", "在庫金額", "推奨発注量"],
        products.map(p => [p.sku, `<button class="link" onclick="loadLedger(${p.id})">${p.product_name}</button>`, p.required_stock_level, p.stock_quantity, status(p.status), yen.format(p.stock_value), p.recommended_order_quantity]))
        + `<div class="table-total"><span>在庫一覧 合計</span><strong>${yen.format(listTotal)}</strong><span>${diffText}</span></div>`
        + `<p class="note">在庫一覧の必要水準は、適正在庫シミュレーションと同じ直近30日予測ベースです。必要水準 = リードタイム需要 + 安全在庫。推奨発注量 = max(必要水準 - 現在在庫, 0) です。</p>`;
    }

    function renderMonthlySummary(elementId, rows, total, totalLabel) {
      document.getElementById(elementId).innerHTML = table(["SKU", "商品", "数量", "金額"],
        rows.map(row => [row.sku, row.product_name, row.quantity, yen.format(row.amount)]))
        + `<div class="table-total"><span>${totalLabel}</span><strong>${yen.format(total || 0)}</strong></div>`;
    }

    async function loadForecast() {
      const horizon = document.getElementById("forecastHorizon").value;
      const data = await api(`/api/forecast-simulation?horizon_days=${horizon}`);
      renderForecast(data);
    }

    function renderForecast(data) {
      document.getElementById("forecastNote").textContent =
        `${data.start_date} から ${data.end_date} までの販売実績を使い、${data.month_end} までの残り ${data.days_to_month_end} 日を予測しています。リードタイム需要は「発注から入荷までに売れそうな数量」、必要在庫は「リードタイム需要 + 安全在庫」です。`;
      document.getElementById("forecastSimulation").innerHTML = table(
        ["SKU", "商品", "現在在庫", "期間販売数", "日次平均", "季節係数", "リードタイム日数", "リードタイム需要", "安全在庫", "必要在庫", "今すぐ推奨発注量", "リードタイム判定", "月末までの予測販売数", "月末在庫見込み", "月末不足数", "月末判定"],
        data.rows.map(row => [
          row.sku,
          row.product_name,
          row.stock_quantity,
          row.recent_sales_quantity,
          row.daily_average,
          row.seasonal_factor,
          row.lead_time_days,
          row.lead_time_demand,
          row.safety_stock,
          row.required_inventory,
          row.recommended_order_quantity,
          forecastJudgement(row.lead_time_judgement),
          row.month_end_forecast,
          row.projected_month_end_stock_after_order,
          row.month_end_shortage,
          forecastJudgement(row.month_end_judgement)
        ])
      );
    }

    function forecastJudgement(text) {
      const cls = (text === "発注不要" || text === "月末OK") ? "ok" : (text === "データ不足" ? "warn" : "danger");
      return `<span class="status ${cls}">${text}</span>`;
    }

    function renderSelects(products) {
      const html = products.map(p => `<option value="${p.id}">${p.sku} ${p.product_name}</option>`).join("");
      document.querySelectorAll("select[name='product_id']").forEach(select => {
        const currentValue = select.value;
        select.innerHTML = html;
        if (currentValue && [...select.options].some(option => option.value === currentValue)) {
          select.value = currentValue;
        }
        select.onchange = () => loadLedger(select.value);
      });
      const ledgerSelect = document.getElementById("ledgerProductSelect");
      if (ledgerSelect) {
        const currentValue = String(currentLedgerProductId || ledgerSelect.value || "");
        ledgerSelect.innerHTML = html;
        if (currentValue && [...ledgerSelect.options].some(option => option.value === currentValue)) {
          ledgerSelect.value = currentValue;
        }
      }
    }

    async function loadPartners() {
      currentPartners = await api("/api/business-partners");
      renderPartnerSelects();
    }

    function renderPartnerSelects() {
      const configs = [
        { selector: "#purchaseForm select[name='partner_name']", rows: currentPartners.suppliers || [] },
        { selector: "#saleForm select[name='partner_name']", rows: currentPartners.customers || [] }
      ];
      for (const config of configs) {
        const select = document.querySelector(config.selector);
        if (!select) continue;
        const currentValue = select.value;
        select.innerHTML = config.rows
          .map(name => `<option value="${escapeAttr(name)}">${escapeHtml(name)}</option>`)
          .join("");
        if (currentValue && config.rows.includes(currentValue)) {
          select.value = currentValue;
        }
      }
    }

    async function addPartner(partnerType, inputId, formId) {
      const input = document.getElementById(inputId);
      const partnerName = input.value.trim();
      if (!partnerName) {
        document.getElementById("message").textContent = "取引先名を入力してください";
        return;
      }
      await api("/api/business-partners", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ partner_type: partnerType, partner_name: partnerName })
      });
      input.value = "";
      await loadPartners();
      const select = document.querySelector(`#${formId} select[name='partner_name']`);
      if (select) select.value = partnerName;
      document.getElementById("message").textContent = "取引先を追加しました";
    }

    function renderQueue(rows) {
      document.getElementById("queue").innerHTML = table(["ID", "元データ", "区分", "状態", "操作"],
        rows.map(q => [
          q.id,
          `${q.source_type} #${q.source_id}`,
          q.direction,
          q.sync_error_message ? `${q.status}<br><span class="error">${q.sync_error_message}</span>` : q.status,
          queueActions(q)
        ]));
    }

    function queueActions(q) {
      const previewButton = `<button onclick="preview('${q.source_type}', ${q.source_id})">確認</button>`;
      if (q.status === "sent") {
        return `${previewButton} <span class="match">送信済み ${q.external_accounting_id || ""}</span>`;
      }
      const label = q.status === "failed" ? "再送" : "疑似freeeへ送信";
      return `${previewButton} <button class="warning" onclick="sendToPseudoFreee(${q.id})">${label}</button>`;
    }

    async function loadLedger(productId) {
      const data = await api(`/api/products/${productId}/ledger`);
      const product = data.product;
      currentLedgerProductId = product.id;
      currentLedgerData = data;
      ledgerExpanded = false;
      renderLedger();
    }

    function renderLedger() {
      if (!currentLedgerData) return;
      const product = currentLedgerData.product;
      const rows = currentLedgerData.ledger;
      const visibleRows = ledgerExpanded ? rows : rows.slice(0, 10);
      const ledgerSelect = document.getElementById("ledgerProductSelect");
      if (ledgerSelect && String(ledgerSelect.value) !== String(product.id)) {
        ledgerSelect.value = product.id;
      }
      document.getElementById("ledgerTitle").textContent = `${product.sku} ${product.product_name} の在庫元帳`;
      document.getElementById("ledgerNote").textContent = ledgerExpanded
        ? `全${rows.length}行を日付の新しい順に表示しています。この元帳の残高が、在庫一覧の現在庫として集計されています。現在の在庫残高金額は ${yen.format(product.inventory_balance_amount)} です。`
        : `最新${visibleRows.length}行のみを日付の新しい順に表示しています。全${rows.length}行を確認する場合は「すべて表示」を押してください。現在の在庫残高金額は ${yen.format(product.inventory_balance_amount)} です。`;
      document.getElementById("ledger").innerHTML = table(
        ["日付", "区分", "取引先", "請求書/注文番号", "入庫", "出庫", "残高", "単価", "取引金額", "在庫残高金額", "freee状態", "操作"],
        visibleRows.map(r => [
          r.movement_date,
          movementLabel(r.movement_type),
          r.partner_name || "-",
          r.invoice_no || "-",
          r.in_quantity || "",
          r.out_quantity || "",
          r.balance,
          yen.format(r.unit_price),
          yen.format(r.amount),
          yen.format(r.inventory_balance_amount),
          r.queue_status || r.accounting_status || "-",
          ledgerAction(r)
        ])
      ) + ledgerToggle(rows.length);
    }

    function ledgerToggle(totalRows) {
      if (totalRows <= 10) return "";
      const label = ledgerExpanded ? "最新10件のみ表示" : "すべて表示";
      return `<div class="table-total"><span>${ledgerExpanded ? "全件表示中" : "折りたたみ表示中"}</span><button class="secondary" onclick="toggleLedger()">${label}</button></div>`;
    }

    function toggleLedger() {
      ledgerExpanded = !ledgerExpanded;
      renderLedger();
    }

    function movementLabel(value) {
      return {
        initial_stock: "初期在庫",
        purchase_receipt: "仕入入庫",
        sale_shipment: "売上出庫",
        purchase_cancel: "仕入取消",
        sale_cancel: "売上取消"
      }[value] || value;
    }

    function ledgerAction(row) {
      if (row.is_correction) return "訂正行";
      if (row.is_cancelled) return "取消済み";
      if (row.source_type !== "purchase" && row.source_type !== "sale") return "-";
      return `<button class="warning" onclick="cancelMovement(${row.id})">取消</button>`;
    }

    async function cancelMovement(movementId) {
      const reason = prompt("取消理由を入力してください", "入力ミスのため取消");
      if (!reason) return;
      await api("/api/inventory-movements/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ movement_id: movementId, reason })
      });
      document.getElementById("message").textContent = "元帳に取消行を追加しました";
      await loadAll();
    }

    function table(headers, rows) {
      return `<table><thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function escapeAttr(value) {
      return escapeHtml(value);
    }

    function status(text) {
      const cls = text === "正常" ? "ok" : (text === "欠品" ? "danger" : "warn");
      return `<span class="status ${cls}">${text}</span>`;
    }

    function showTransactionForm(formId) {
      document.querySelectorAll(".transaction-form").forEach(form => form.classList.toggle("active", form.id === formId));
      document.querySelectorAll(".form-tab").forEach(tab => tab.classList.toggle("active", tab.dataset.form === formId));
    }

    async function submitForm(form, path) {
      const data = Object.fromEntries(new FormData(form).entries());
      const productId = data.product_id;
      const result = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
      document.getElementById("message").textContent = result.ok ? "登録しました" : "";
      form.reset();
      for (const input of form.querySelectorAll('input[type="date"][required]')) input.value = today;
      await loadAll();
      await loadLedger(productId);
    }

    async function preview(sourceType, sourceId) {
      const data = await api(`/api/freee-preview?source_type=${sourceType}&source_id=${sourceId}`);
      document.getElementById("preview").textContent = JSON.stringify(data, null, 2);
    }

    async function sendToPseudoFreee(id) {
      try {
        const result = await api("/api/freee-sync-queue/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
        document.getElementById("message").textContent = `疑似freeeへ送信しました: ${result.external_accounting_id}`;
      } catch (error) {
        document.getElementById("message").textContent = error.message;
      }
      await loadAll();
    }

    document.getElementById("purchaseForm").addEventListener("submit", async event => {
      event.preventDefault();
      try { await submitForm(event.target, "/api/purchases"); } catch (error) { document.getElementById("message").textContent = error.message; }
    });
    document.getElementById("saleForm").addEventListener("submit", async event => {
      event.preventDefault();
      try { await submitForm(event.target, "/api/sales"); } catch (error) { document.getElementById("message").textContent = error.message; }
    });
    loadAll().catch(error => document.getElementById("message").textContent = error.message);
  </script>
</body>
</html>
"""


def run() -> None:
    init_db()
    server = ThreadingHTTPServer((HOST, PORT), InventoryHandler)
    print(f"Inventory dashboard running at http://{HOST}:{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    run()
