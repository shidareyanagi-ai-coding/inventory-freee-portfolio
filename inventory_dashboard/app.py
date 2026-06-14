from __future__ import annotations

import json
import math
import os
import urllib.error
import urllib.request
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

import db
from index_html import INDEX_HTML

try:
    from dotenv import load_dotenv

    load_dotenv()  # .env があれば DATABASE_URL などを読み込む（無ければ何もしない）
except Exception:
    pass


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "inventory.db"
HOST = "127.0.0.1"
PORT = int(os.environ.get("INVENTORY_DASHBOARD_PORT", "8000"))
DEMO_HISTORY_MONTHS = 24
PSEUDO_FREEE_API_URL = os.environ.get("PSEUDO_FREEE_API_URL", "http://127.0.0.1:8010").rstrip("/")


# スキーマ DDL・接続・SQL方言差の吸収は db.py（DBアクセス層, A-2）が所有する。
# ここ(app.py)は業務ロジックに徹し、常に '?' プレースホルダで SQL を書く。


def get_conn() -> Any:
    """DB アクセス層(db.py)へ委譲。DATABASE_URL で SQLite/Postgres を切替える。

    SQLite のパスは呼び出し時の DB_PATH を渡す（テストはこの DB_PATH を差し替える）。
    """
    return db.get_conn(DB_PATH)


def init_db() -> None:
    with get_conn() as conn:
        db.create_schema(conn)
        count = conn.execute("SELECT COUNT(*) AS count FROM products").fetchone()["count"]
        if count == 0:
            seed_products(conn)
        normalize_initial_stock_dates(conn)
        ensure_sample_transactions(conn)
        ensure_demo_history(conn)
        sync_partner_master(conn)


def seed_products(conn: db.Connection) -> None:
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


def normalize_initial_stock_dates(conn: db.Connection) -> None:
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


def ensure_sample_transactions(conn: db.Connection) -> None:
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


def ensure_demo_history(conn: db.Connection) -> None:
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


def insert_demo_purchase(conn: db.Connection, product: dict[str, Any], purchase_date: date, quantity: int) -> None:
    invoice_no = f"DEMO-HIST-P-{product['sku']}-{purchase_date:%Y%m}"
    created_at = purchase_date.isoformat()
    purchase_id = db.insert_returning_id(
        conn,
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
    conn: db.Connection,
    product: dict[str, Any],
    partner_name: str,
    sale_date: date,
    quantity: int,
    suffix: str,
) -> None:
    invoice_no = f"DEMO-HIST-S-{product['sku']}-{sale_date:%Y%m}-{suffix}"
    created_at = sale_date.isoformat()
    sale_id = db.insert_returning_id(
        conn,
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


def stock_by_product(conn: db.Connection) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT product_id, COALESCE(SUM(quantity_delta), 0) AS stock_quantity
        FROM inventory_movements
        GROUP BY product_id
        """
    ).fetchall()
    return {row["product_id"]: int(row["stock_quantity"]) for row in rows}


def list_products(conn: db.Connection) -> list[dict[str, Any]]:
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


def sync_partner_master(conn: db.Connection) -> None:
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


def add_business_partner(conn: db.Connection, partner_type: str, partner_name: str) -> None:
    if partner_type not in {"supplier", "customer"}:
        raise ValueError("invalid partner_type")
    name = required_text(partner_name, "partner_name")
    # INSERT OR IGNORE は SQLite 方言。両対応の "ON CONFLICT DO NOTHING" に統一する
    # （UNIQUE(partner_type, partner_name) 衝突時は黙って無視）。
    conn.execute(
        """
        INSERT INTO business_partners (partner_type, partner_name)
        VALUES (?, ?)
        ON CONFLICT DO NOTHING
        """,
        (partner_type, name),
    )


def list_business_partners(conn: db.Connection) -> dict[str, list[str]]:
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


def create_business_partner(conn: db.Connection, data: dict[str, Any]) -> dict[str, Any]:
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


def create_product(conn: db.Connection, data: dict[str, Any]) -> dict[str, Any]:
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


def create_purchase(conn: db.Connection, data: dict[str, Any]) -> dict[str, Any]:
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

    purchase_id = db.insert_returning_id(
        conn,
        """
        INSERT INTO purchases (
            product_id, partner_name, invoice_no, transaction_date, received_date,
            quantity, unit_price, tax_rate, tax_category, due_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (product["id"], partner_name, invoice_no, transaction_date, received_date, quantity, unit_price, tax_rate, tax_category, due_date),
    )
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


def create_sale(conn: db.Connection, data: dict[str, Any]) -> dict[str, Any]:
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

    sale_id = db.insert_returning_id(
        conn,
        """
        INSERT INTO sales (
            product_id, partner_name, invoice_no, transaction_date,
            quantity, unit_price, tax_rate, tax_category, due_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (product["id"], partner_name, invoice_no, transaction_date, quantity, unit_price, tax_rate, tax_category, due_date),
    )
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


def enqueue_freee_payload(conn: db.Connection, source_type: str, source_id: int) -> None:
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


def build_freee_payload(conn: db.Connection, source_type: str, source_id: int) -> dict[str, Any]:
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


def get_product(conn: db.Connection, product_id: int) -> dict[str, Any]:
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        raise ValueError("product not found")
    return product


def dashboard(conn: db.Connection) -> dict[str, Any]:
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


def forecast_simulation(conn: db.Connection, horizon_days: int = 30) -> dict[str, Any]:
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


def active_sales_quantity(conn: db.Connection, product_id: int, start_date: str, end_date: str) -> int:
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


def monthly_seasonal_factor(conn: db.Connection, product_id: int, target_month: int) -> float:
    # 月の抽出は方言差があるため db.month_expr で吸収（strftime ⇄ EXTRACT）。
    month_sql = db.month_expr(conn, "s.transaction_date")
    rows = conn.execute(
        f"""
        SELECT {month_sql} AS month,
               SUM(s.quantity) AS quantity
        FROM sales s
        JOIN inventory_movements im ON im.source_type = 'sale' AND im.source_id = s.id
        LEFT JOIN inventory_corrections c ON c.original_movement_id = im.id
        WHERE s.product_id = ?
          AND c.id IS NULL
        GROUP BY {month_sql}
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


def product_ledger(conn: db.Connection, product_id: int) -> dict[str, Any]:
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


def cancel_inventory_movement(conn: db.Connection, data: dict[str, Any]) -> dict[str, Any]:
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
    correction_movement_id = db.insert_returning_id(
        conn,
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


def list_queue(conn: db.Connection) -> list[dict[str, Any]]:
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


def mark_queue_status(conn: db.Connection, data: dict[str, Any]) -> dict[str, Any]:
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


def fail_queue_send(conn: db.Connection, queue_id: int, message: str) -> None:
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


def send_queue_to_pseudo_freee(conn: db.Connection, data: dict[str, Any]) -> dict[str, Any]:
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


# ---------------------------------------------------------------------------
# Web 層（FastAPI）
# ---------------------------------------------------------------------------
# stdlib の InventoryHandler を撤去し FastAPI へ移行（EVOLUTION_PLAN.md A-1）。
# 上の業務ロジック関数（create_purchase / forecast_simulation など）はそのまま
# 再利用し、この層は「HTTP を業務関数へ橋渡しする薄い層」に徹する。
# DB アクセス層の分離（db.py）と Postgres 化は A-2 で行う。


@asynccontextmanager
async def lifespan(app: FastAPI) -> Any:
    # 起動時に SQLite スキーマ作成とデモデータ投入を済ませる（旧 run() と同じ役割）。
    init_db()
    yield


app = FastAPI(title="在庫管理ダッシュボード API", lifespan=lifespan)


def parse_json_body(payload: dict[str, Any] | None = Body(default=None)) -> dict[str, Any]:
    """リクエストボディ(JSON)を dict として受け取り、業務ロジック関数へそのまま渡す。

    入力検証は各業務関数（required_text / positive_int / 在庫チェック等）が一手に担うため、
    ここでは Pydantic の厳密モデルを敢えて使わず重複検証を避ける。
    型付きリクエストモデルは OpenAPI から TS 型を生成する Plan B で導入予定。
    """
    return payload or {}


# --- エラー整形 -------------------------------------------------------------
# フロント（index_html.py 内の api()）は失敗時に res.json().error を読む。
# 旧実装の「{"error": ...} + 4xx」という契約を維持するため、例外を整形して返す。
@app.exception_handler(ValueError)
async def handle_value_error(request: Request, exc: ValueError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": str(exc)})


async def handle_integrity_error(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": f"database integrity error: {exc}"})


# IntegrityError は使用中のバックエンドで型が異なる（sqlite3 / psycopg）。
# db.INTEGRITY_ERROR_TYPES の各型に同じハンドラを登録する。
for _integrity_error_type in db.INTEGRITY_ERROR_TYPES:
    app.add_exception_handler(_integrity_error_type, handle_integrity_error)


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    # 404 等も {"error": ...} 形に揃える（旧実装は未知パスに {"error": "not found"} を返した）。
    detail = "not found" if exc.status_code == 404 else exc.detail
    return JSONResponse(status_code=exc.status_code, content={"error": detail})


# --- 画面 -------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML


# --- 参照系 API -------------------------------------------------------------
@app.get("/api/dashboard")
def api_dashboard() -> dict[str, Any]:
    with get_conn() as conn:
        return dashboard(conn)


@app.get("/api/products")
def api_products() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return list_products(conn)


@app.get("/api/business-partners")
def api_business_partners() -> dict[str, Any]:
    with get_conn() as conn:
        return list_business_partners(conn)


@app.get("/api/forecast-simulation")
def api_forecast_simulation(horizon_days: int = 30) -> dict[str, Any]:
    with get_conn() as conn:
        return forecast_simulation(conn, horizon_days)


@app.get("/api/products/{product_id}/ledger")
def api_product_ledger(product_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        return product_ledger(conn, product_id)


@app.get("/api/freee-sync-queue")
def api_freee_sync_queue() -> list[dict[str, Any]]:
    with get_conn() as conn:
        return list_queue(conn)


@app.get("/api/freee-preview")
def api_freee_preview(source_type: str = "", source_id: int = 0) -> dict[str, Any]:
    with get_conn() as conn:
        return build_freee_payload(conn, source_type, source_id)


# --- 更新系 API（成功時 201 Created）---------------------------------------
@app.post("/api/products", status_code=201)
def api_create_product(data: dict[str, Any] = Depends(parse_json_body)) -> dict[str, Any]:
    with get_conn() as conn:
        return create_product(conn, data)


@app.post("/api/purchases", status_code=201)
def api_create_purchase(data: dict[str, Any] = Depends(parse_json_body)) -> dict[str, Any]:
    with get_conn() as conn:
        return create_purchase(conn, data)


@app.post("/api/sales", status_code=201)
def api_create_sale(data: dict[str, Any] = Depends(parse_json_body)) -> dict[str, Any]:
    with get_conn() as conn:
        return create_sale(conn, data)


@app.post("/api/business-partners", status_code=201)
def api_create_business_partner(data: dict[str, Any] = Depends(parse_json_body)) -> dict[str, Any]:
    with get_conn() as conn:
        return create_business_partner(conn, data)


@app.post("/api/freee-sync-queue/send", status_code=201)
def api_send_queue(data: dict[str, Any] = Depends(parse_json_body)) -> dict[str, Any]:
    with get_conn() as conn:
        return send_queue_to_pseudo_freee(conn, data)


@app.post("/api/freee-sync-queue/status", status_code=201)
def api_mark_queue_status(data: dict[str, Any] = Depends(parse_json_body)) -> dict[str, Any]:
    with get_conn() as conn:
        return mark_queue_status(conn, data)


@app.post("/api/inventory-movements/cancel", status_code=201)
def api_cancel_movement(data: dict[str, Any] = Depends(parse_json_body)) -> dict[str, Any]:
    with get_conn() as conn:
        return cancel_inventory_movement(conn, data)


# ---------------------------------------------------------------------------
def run() -> None:
    import uvicorn

    print(f"Inventory dashboard running at http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    run()

