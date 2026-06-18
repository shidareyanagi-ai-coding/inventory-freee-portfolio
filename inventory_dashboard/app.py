from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request
from calendar import monthrange
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, Header, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

import ai_capture
import auth
import db
import storage
from forecasting import synthetic
from index_html import render_index


class NotFoundError(Exception):
    """対象リソースが存在しない／別テナントのため見えない（404 相当）。

    別テナントの id を渡されたときも「存在しない」と区別なく 404 を返すことで、
    リソースの有無を漏らさない（IDOR 対策）。
    """


class ForbiddenError(Exception):
    """認証済みだが権限不足（403 相当）。RBAC（viewer は更新系不可など）。"""

try:
    from dotenv import load_dotenv

    load_dotenv()  # .env があれば DATABASE_URL などを読み込む（無ければ何もしない）
except Exception:
    pass


APP_DIR = Path(__file__).resolve().parent
DB_PATH = APP_DIR / "inventory.db"
# A-5: 証憑の元画像。DBには相対パス(storage_path)のみを持つ。
# A-6: 実際の置き場所は storage.py が決める。env の STORAGE_* があれば R2 等の
#   オブジェクトストレージ、無ければこの VOUCHER_DIR（ローカルフォルダ・.gitignore 済み）。
VOUCHER_DIR = APP_DIR / "voucher_store"
# 本番(Render等)は環境変数 PORT で待ち受けポートが渡され、外部公開のため 0.0.0.0 にバインドする。
# ローカルは従来どおり 127.0.0.1。PORT が来ているか（=クラウド上か）で自動で切り替える（A-6）。
PORT = int(os.environ.get("PORT") or os.environ.get("INVENTORY_DASHBOARD_PORT", "8000"))
HOST = os.environ.get("INVENTORY_DASHBOARD_HOST") or ("0.0.0.0" if os.environ.get("PORT") else "127.0.0.1")
DEMO_HISTORY_MONTHS = 24
# A-6: デモ組織の seed 直後に予測バッチを流し「常にAI版（必要在庫）」を保証する。
# テストは速度のため setUp で False にする（毎回の重い学習を避ける）。
RUN_FORECAST_ON_SEED = True
PSEUDO_FREEE_API_URL = os.environ.get("PSEUDO_FREEE_API_URL", "http://127.0.0.1:8010").rstrip("/")


# スキーマ DDL・接続・SQL方言差の吸収は db.py（DBアクセス層, A-2）が所有する。
# ここ(app.py)は業務ロジックに徹し、常に '?' プレースホルダで SQL を書く。


def get_conn() -> Any:
    """DB アクセス層(db.py)へ委譲。DATABASE_URL で SQLite/Postgres を切替える。

    SQLite のパスは呼び出し時の DB_PATH を渡す（テストはこの DB_PATH を差し替える）。
    """
    return db.get_conn(DB_PATH)


def init_db() -> None:
    """起動時はスキーマ作成だけ。デモ seed は「初回ログインで自組織に」行う(A-3)。"""
    with get_conn() as conn:
        db.create_schema(conn)
        db.assert_tenancy_ready(conn)


# ---------------------------------------------------------------------------
# テナント＆権限（A-3）
# ---------------------------------------------------------------------------
# 「1サインアップ=1組織」。Clerk のユーザ(sub)を memberships で自前 organization へ
# 紐付け、初回ログイン時に自組織サンドボックスを作ってデモ seed を入れる。
# 認可（organization_id 絞り込み・ロール判定）はこのサーバ側が単一の主体。


@dataclass
class Identity:
    """1リクエストの「誰が・どの組織で・どの権限で」を表す。"""

    organization_id: int
    user_id: str
    role: str


def create_organization(conn: db.Connection, name: str) -> int:
    return db.insert_returning_id(
        conn,
        "INSERT INTO organizations (name) VALUES (?)",
        (name,),
    )


def get_membership_by_user(conn: db.Connection, user_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "SELECT * FROM memberships WHERE user_id = ? ORDER BY id LIMIT 1",
        (user_id,),
    ).fetchone()


def set_membership(conn: db.Connection, organization_id: int, user_id: str, role: str) -> None:
    """user_id を organization に role で所属させる（無ければ作成、あれば role 更新）。"""
    if role not in {"admin", "staff", "viewer"}:
        raise ValueError("invalid role")
    existing = conn.execute(
        "SELECT id FROM memberships WHERE organization_id = ? AND user_id = ?",
        (organization_id, user_id),
    ).fetchone()
    if existing:
        conn.execute("UPDATE memberships SET role = ? WHERE id = ?", (role, existing["id"]))
    else:
        conn.execute(
            "INSERT INTO memberships (organization_id, user_id, role) VALUES (?, ?, ?)",
            (organization_id, user_id, role),
        )


def provision_organization_for_user(conn: db.Connection, user_id: str) -> Identity:
    """初回ログイン: 自組織サンドボックスを作り、admin 権限とデモ seed を用意する。"""
    organization_id = create_organization(conn, f"{user_id} のサンドボックス")
    set_membership(conn, organization_id, user_id, "admin")
    seed_organization(conn, organization_id)
    record_audit(conn, organization_id, user_id, "organization.provisioned", "organization", organization_id)
    return Identity(organization_id=organization_id, user_id=user_id, role="admin")


def resolve_identity(user_id: str) -> Identity:
    """user_id(Clerk sub) から Identity を解決する。初回ログインなら組織を作成して seed。"""
    with get_conn() as conn:
        membership = get_membership_by_user(conn, user_id)
        if membership:
            return Identity(
                organization_id=int(membership["organization_id"]),
                user_id=user_id,
                role=str(membership["role"]),
            )
        return provision_organization_for_user(conn, user_id)


def record_audit(
    conn: db.Connection,
    organization_id: int,
    actor_user_id: str,
    action: str,
    target_type: str = "",
    target_id: Any = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """監査ログ（誰が・いつ・何を）。失敗しても業務処理は止めない方針ではなく、
    同一トランザクション内に必ず残す（操作と監査の整合を担保する）。"""
    conn.execute(
        """
        INSERT INTO audit_logs (organization_id, actor_user_id, action, target_type, target_id, detail_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            organization_id,
            actor_user_id or "",
            action,
            target_type,
            str(target_id),
            json.dumps(detail or {}, ensure_ascii=False),
        ),
    )


def list_audit_logs(conn: db.Connection, organization_id: int, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, actor_user_id, action, target_type, target_id, detail_json, created_at
        FROM audit_logs
        WHERE organization_id = ?
        ORDER BY id DESC
        LIMIT ?
        """,
        (organization_id, limit),
    ).fetchall()
    for row in rows:
        row["detail"] = json.loads(row["detail_json"])
    return rows


def seed_organization(conn: db.Connection, organization_id: int) -> None:
    """指定 organization にデモデータ（商品・取引・24ヶ月履歴・取引先）を投入する。

    旧 init_db のグローバル seed を「組織単位」に切り出したもの。冪等。
    """
    count = conn.execute(
        "SELECT COUNT(*) AS count FROM products WHERE organization_id = ?",
        (organization_id,),
    ).fetchone()["count"]
    if count == 0:
        seed_products(conn, organization_id)
    normalize_initial_stock_dates(conn, organization_id)
    ensure_sample_transactions(conn, organization_id)
    ensure_demo_history(conn, organization_id)
    sync_partner_master(conn, organization_id)
    # A-6: 「常にAI版」のため、デモ投入直後に予測バッチを1回流して AI 必要在庫を用意する。
    if RUN_FORECAST_ON_SEED:
        try:
            from forecasting import service as forecast_service

            forecast_service.run_forecast(conn, organization_id, horizon_days=30)
        except Exception:
            pass  # 予測失敗でも seed 自体は成立させる（次回「予測バッチを実行」で補完）


def seed_products(conn: db.Connection, organization_id: int) -> None:
    products = [
        ("SKU-USB-C-001", "USB-Cケーブル 1m", "ケーブル", "東京サプライ", 480, 980, 10, 5, 20, 30, 10),
        ("SKU-MOUSE-001", "ワイヤレスマウス", "周辺機器", "関東OA商事", 1200, 2480, 10, 7, 10, 18, 5),
        ("SKU-MONITOR-024", "24インチモニター", "PC関連", "関東OA商事", 13500, 19800, 10, 14, 4, 8, 2),
    ]
    conn.executemany(
        """
        INSERT INTO products (
            organization_id, sku, product_name, category, supplier_name, purchase_unit_price,
            sales_unit_price, tax_rate, lead_time_days, safety_stock,
            reorder_point, min_order_quantity
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [(organization_id, *product) for product in products],
    )
    rows = conn.execute(
        "SELECT id, product_name, purchase_unit_price FROM products WHERE organization_id = ?",
        (organization_id,),
    ).fetchall()
    initial_stock = {"USB-Cケーブル 1m": 25, "ワイヤレスマウス": 12, "24インチモニター": 6}
    for row in rows:
        conn.execute(
            """
            INSERT INTO inventory_movements (
                organization_id, product_id, movement_type, source_type, source_id, movement_date,
                quantity_delta, unit_price, note
            )
            VALUES (?, ?, 'initial_stock', 'seed', ?, ?, ?, ?, '初期在庫')
            """,
            (organization_id, row["id"], row["id"], initial_stock_date(), initial_stock[row["product_name"]], row["purchase_unit_price"]),
        )


def initial_stock_date() -> str:
    return add_months(date.today().replace(day=1), -DEMO_HISTORY_MONTHS).isoformat()


def normalize_initial_stock_dates(conn: db.Connection, organization_id: int) -> None:
    stock_date = initial_stock_date()
    conn.execute(
        """
        UPDATE inventory_movements
        SET movement_date = ?
        WHERE organization_id = ?
          AND movement_type = 'initial_stock'
          AND source_type = 'seed'
          AND movement_date <> ?
        """,
        (stock_date, organization_id, stock_date),
    )


def ensure_sample_transactions(conn: db.Connection, organization_id: int) -> None:
    purchase_count = conn.execute(
        "SELECT COUNT(*) AS count FROM purchases WHERE organization_id = ?", (organization_id,)
    ).fetchone()["count"]
    sale_count = conn.execute(
        "SELECT COUNT(*) AS count FROM sales WHERE organization_id = ?", (organization_id,)
    ).fetchone()["count"]
    if purchase_count or sale_count:
        return

    products = {
        row["sku"]: row
        for row in conn.execute(
            "SELECT * FROM products WHERE organization_id = ?", (organization_id,)
        ).fetchall()
    }
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
            create_purchase(conn, organization_id, data)
        else:
            create_sale(conn, organization_id, data)


def ensure_demo_history(conn: db.Connection, organization_id: int) -> None:
    """需要予測レベル2(A-4)向けに「2年・日次」のデモ履歴を投入する（冪等）。

    旧実装は月次2点の粗いデータだった。ここでは forecasting.synthetic で
    トレンド＋週次/月次季節＋補助金/キャンペーンのスパイク＋ノイズを持つ日次需要を作り、
    (s,S) 補充で在庫を正に保ちながら 売上・仕入・在庫移動・external_factors を投入する。
    Neon でも速いよう executemany ＋ INSERT...SELECT でバッチ化し、行単位 RETURNING を避ける。
    """
    exists = conn.execute(
        "SELECT id FROM sales WHERE organization_id = ? AND invoice_no LIKE 'DEMO-HIST-S-%' LIMIT 1",
        (organization_id,),
    ).fetchone()
    if exists:
        return

    products = {
        row["sku"]: row
        for row in conn.execute(
            "SELECT * FROM products WHERE organization_id = ?", (organization_id,)
        ).fetchall()
    }
    # 1日あたりの base に作り直した日次パターン（月次版の base/12 相当）。
    patterns = {
        "SKU-USB-C-001": {"base": 9.0, "partner": "青山ECストア", "season": 1.25},
        "SKU-MOUSE-001": {"base": 5.0, "partner": "新宿デザイン事務所", "season": 1.15},
        "SKU-MONITOR-024": {"base": 1.6, "partner": "日本橋システムズ", "season": 1.4},
    }

    today = date.today()
    # 初期在庫日の翌日〜「昨日」を日次で埋める（初期在庫がデモ最古日より必ず前になる）。
    start = date.fromisoformat(initial_stock_date()) + timedelta(days=1)
    end = today - timedelta(days=1)
    if end < start:
        return

    # 組織横断イベント（補助金/キャンペーン）。需要スパイクの種＋LightGBM の特徴量源。
    events = synthetic.generate_events(start, end)
    factor_rows = [
        (organization_id, factor_date, factor_type, None, 1.0, f"デモ{factor_type}")
        for factor_date, factor_type in sorted(events.items())
    ]
    if factor_rows:
        conn.executemany(
            """
            INSERT INTO external_factors
                (organization_id, factor_date, factor_type, product_id, value, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            factor_rows,
        )

    # 初期在庫（seed_products が入れた initial_stock 移動）を起点に在庫を追跡する。
    initial_stock = {
        row["product_id"]: int(row["stock"])
        for row in conn.execute(
            """
            SELECT product_id, COALESCE(SUM(quantity_delta), 0) AS stock
            FROM inventory_movements
            WHERE organization_id = ? AND movement_type = 'initial_stock'
            GROUP BY product_id
            """,
            (organization_id,),
        ).fetchall()
    }

    sales_rows: list[tuple[Any, ...]] = []
    purchase_rows: list[tuple[Any, ...]] = []
    cover_days = 30

    for sku, pattern in patterns.items():
        product = products[sku]
        demand = synthetic.daily_demand_series(
            pattern["base"], pattern["season"], start, end, events, seed=synthetic.seed_for_sku(sku)
        )
        stock = initial_stock.get(product["id"], 0)
        reorder_level = int(product["reorder_point"]) + int(product["safety_stock"])
        order_quantity = max(int(product["min_order_quantity"]), int(math.ceil(cover_days * pattern["base"])))

        for movement_date, quantity in demand:
            date_iso = movement_date.isoformat()
            due_date = safe_date(movement_date.year, movement_date.month, 28).isoformat()
            # (s,S) 補充: 必要水準以下なら cover_days 分まとめて仕入れる（在庫を正に保つ）。
            if stock <= reorder_level:
                purchase_rows.append(
                    (
                        organization_id,
                        product["id"],
                        product["supplier_name"],
                        f"DEMO-HIST-P-{sku}-{movement_date:%Y%m%d}",
                        date_iso,
                        date_iso,
                        order_quantity,
                        product["purchase_unit_price"],
                        product["tax_rate"],
                        "課税仕入 10%",
                        due_date,
                        "demo",
                        date_iso,
                    )
                )
                stock += order_quantity
            sell = min(quantity, stock)
            if sell > 0:
                sales_rows.append(
                    (
                        organization_id,
                        product["id"],
                        pattern["partner"],
                        f"DEMO-HIST-S-{sku}-{movement_date:%Y%m%d}",
                        date_iso,
                        sell,
                        product["sales_unit_price"],
                        product["tax_rate"],
                        "課税売上 10%",
                        due_date,
                        "demo",
                        date_iso,
                    )
                )
                stock -= sell

    if purchase_rows:
        conn.executemany(
            """
            INSERT INTO purchases (
                organization_id, product_id, partner_name, invoice_no, transaction_date, received_date,
                quantity, unit_price, tax_rate, tax_category, due_date,
                external_accounting_status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            purchase_rows,
        )
    if sales_rows:
        conn.executemany(
            """
            INSERT INTO sales (
                organization_id, product_id, partner_name, invoice_no, transaction_date,
                quantity, unit_price, tax_rate, tax_category, due_date,
                external_accounting_status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            sales_rows,
        )

    # 在庫移動は INSERT...SELECT で一括生成（採番済み id を参照。'||' 連結は両方言可）。
    conn.execute(
        """
        INSERT INTO inventory_movements (
            organization_id, product_id, movement_type, source_type, source_id,
            movement_date, quantity_delta, unit_price, note, created_at
        )
        SELECT organization_id, product_id, 'purchase_receipt', 'purchase', id,
               received_date, quantity, unit_price, 'デモ仕入 ' || invoice_no, created_at
        FROM purchases
        WHERE organization_id = ? AND invoice_no LIKE 'DEMO-HIST-P-%'
          AND NOT EXISTS (
              SELECT 1 FROM inventory_movements im
              WHERE im.source_type = 'purchase' AND im.source_id = purchases.id
          )
        """,
        (organization_id,),
    )
    conn.execute(
        """
        INSERT INTO inventory_movements (
            organization_id, product_id, movement_type, source_type, source_id,
            movement_date, quantity_delta, unit_price, note, created_at
        )
        SELECT organization_id, product_id, 'sale_shipment', 'sale', id,
               transaction_date, -quantity, unit_price, 'デモ売上 ' || invoice_no, created_at
        FROM sales
        WHERE organization_id = ? AND invoice_no LIKE 'DEMO-HIST-S-%'
          AND NOT EXISTS (
              SELECT 1 FROM inventory_movements im
              WHERE im.source_type = 'sale' AND im.source_id = sales.id
          )
        """,
        (organization_id,),
    )


def add_months(value: date, months: int) -> date:
    month = value.month - 1 + months
    year = value.year + month // 12
    month = month % 12 + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def safe_date(year: int, month: int, day: int) -> date:
    return date(year, month, min(day, monthrange(year, month)[1]))


def stock_by_product(conn: db.Connection, organization_id: int) -> dict[int, int]:
    rows = conn.execute(
        """
        SELECT product_id, COALESCE(SUM(quantity_delta), 0) AS stock_quantity
        FROM inventory_movements
        WHERE organization_id = ?
        GROUP BY product_id
        """,
        (organization_id,),
    ).fetchall()
    return {row["product_id"]: int(row["stock_quantity"]) for row in rows}


def list_products(conn: db.Connection, organization_id: int) -> list[dict[str, Any]]:
    stocks = stock_by_product(conn, organization_id)
    products = conn.execute(
        "SELECT * FROM products WHERE organization_id = ? ORDER BY id", (organization_id,)
    ).fetchall()
    for product in products:
        stock = stocks.get(product["id"], 0)
        product["stock_quantity"] = stock
        product["stock_value"] = stock * product["purchase_unit_price"]
        product["status"] = stock_status(product)
        product["required_stock_level"] = int(product["reorder_point"]) + int(product["safety_stock"])
        product["recommended_order_quantity"] = recommended_order_quantity(product, stock)
    return products


def sync_partner_master(conn: db.Connection, organization_id: int) -> None:
    supplier_names = [
        row["partner_name"]
        for row in conn.execute(
            """
            SELECT supplier_name AS partner_name FROM products
            WHERE organization_id = ? AND supplier_name <> ''
            UNION
            SELECT partner_name FROM purchases
            WHERE organization_id = ? AND partner_name <> ''
            """,
            (organization_id, organization_id),
        ).fetchall()
    ]
    customer_names = [
        row["partner_name"]
        for row in conn.execute(
            "SELECT DISTINCT partner_name FROM sales WHERE organization_id = ? AND partner_name <> ''",
            (organization_id,),
        ).fetchall()
    ]
    for name in supplier_names:
        add_business_partner(conn, organization_id, "supplier", name)
    for name in customer_names:
        add_business_partner(conn, organization_id, "customer", name)


def add_business_partner(conn: db.Connection, organization_id: int, partner_type: str, partner_name: str) -> None:
    if partner_type not in {"supplier", "customer"}:
        raise ValueError("invalid partner_type")
    name = required_text(partner_name, "partner_name")
    # INSERT OR IGNORE は SQLite 方言。両対応の "ON CONFLICT DO NOTHING" に統一する
    # （UNIQUE(organization_id, partner_type, partner_name) 衝突時は黙って無視）。
    conn.execute(
        """
        INSERT INTO business_partners (organization_id, partner_type, partner_name)
        VALUES (?, ?, ?)
        ON CONFLICT DO NOTHING
        """,
        (organization_id, partner_type, name),
    )


def list_business_partners(conn: db.Connection, organization_id: int) -> dict[str, list[str]]:
    rows = conn.execute(
        """
        SELECT partner_type, partner_name
        FROM business_partners
        WHERE organization_id = ?
        ORDER BY partner_type, partner_name
        """,
        (organization_id,),
    ).fetchall()
    partners = {"suppliers": [], "customers": []}
    for row in rows:
        key = "suppliers" if row["partner_type"] == "supplier" else "customers"
        partners[key].append(row["partner_name"])
    return partners


def create_business_partner(conn: db.Connection, organization_id: int, data: dict[str, Any]) -> dict[str, Any]:
    partner_type = data.get("partner_type", "")
    partner_name = required_text(data.get("partner_name"), "partner_name")
    add_business_partner(conn, organization_id, partner_type, partner_name)
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


def create_product(conn: db.Connection, organization_id: int, data: dict[str, Any]) -> dict[str, Any]:
    required = ["sku", "product_name"]
    for key in required:
        if not str(data.get(key, "")).strip():
            raise ValueError(f"{key} is required")
    supplier_name = data.get("supplier_name", "").strip()
    conn.execute(
        """
        INSERT INTO products (
            organization_id, sku, product_name, category, supplier_name, purchase_unit_price,
            sales_unit_price, tax_rate, tax_category, lead_time_days,
            safety_stock, reorder_point, min_order_quantity
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            organization_id,
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
        add_business_partner(conn, organization_id, "supplier", supplier_name)
    return {"ok": True}


def create_purchase(conn: db.Connection, organization_id: int, data: dict[str, Any]) -> dict[str, Any]:
    product = get_product(conn, organization_id, to_int(data.get("product_id")))
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
            organization_id, product_id, partner_name, invoice_no, transaction_date, received_date,
            quantity, unit_price, tax_rate, tax_category, due_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (organization_id, product["id"], partner_name, invoice_no, transaction_date, received_date, quantity, unit_price, tax_rate, tax_category, due_date),
    )
    conn.execute(
        """
        INSERT INTO inventory_movements (
            organization_id, product_id, movement_type, source_type, source_id, movement_date,
            quantity_delta, unit_price, note
        )
        VALUES (?, ?, 'purchase_receipt', 'purchase', ?, ?, ?, ?, ?)
        """,
        (organization_id, product["id"], purchase_id, received_date, quantity, unit_price, f"仕入 {invoice_no}"),
    )
    enqueue_freee_payload(conn, organization_id, "purchase", purchase_id)
    add_business_partner(conn, organization_id, "supplier", partner_name)
    return {"ok": True, "purchase_id": purchase_id}


def create_sale(conn: db.Connection, organization_id: int, data: dict[str, Any]) -> dict[str, Any]:
    product = get_product(conn, organization_id, to_int(data.get("product_id")))
    quantity = positive_int(data.get("quantity"), "quantity")
    current_stock = stock_by_product(conn, organization_id).get(product["id"], 0)
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
            organization_id, product_id, partner_name, invoice_no, transaction_date,
            quantity, unit_price, tax_rate, tax_category, due_date
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (organization_id, product["id"], partner_name, invoice_no, transaction_date, quantity, unit_price, tax_rate, tax_category, due_date),
    )
    conn.execute(
        """
        INSERT INTO inventory_movements (
            organization_id, product_id, movement_type, source_type, source_id, movement_date,
            quantity_delta, unit_price, note
        )
        VALUES (?, ?, 'sale_shipment', 'sale', ?, ?, ?, ?, ?)
        """,
        (organization_id, product["id"], sale_id, transaction_date, -quantity, unit_price, f"売上 {invoice_no}"),
    )
    enqueue_freee_payload(conn, organization_id, "sale", sale_id)
    add_business_partner(conn, organization_id, "customer", partner_name)
    return {"ok": True, "sale_id": sale_id}


def enqueue_freee_payload(conn: db.Connection, organization_id: int, source_type: str, source_id: int) -> None:
    payload = build_freee_payload(conn, organization_id, source_type, source_id)
    direction = "expense" if source_type == "purchase" else "income"
    conn.execute(
        """
        INSERT INTO freee_sync_queue (organization_id, source_type, source_id, direction, status, payload_json)
        VALUES (?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(source_type, source_id) DO UPDATE SET
            payload_json = excluded.payload_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        (organization_id, source_type, source_id, direction, json.dumps(payload, ensure_ascii=False)),
    )


def build_freee_payload(conn: db.Connection, organization_id: int, source_type: str, source_id: int) -> dict[str, Any]:
    if source_type == "purchase":
        row = conn.execute(
            """
            SELECT p.*, pr.sku, pr.product_name
            FROM purchases p
            JOIN products pr ON pr.id = p.product_id
            WHERE p.id = ? AND p.organization_id = ?
            """,
            (source_id, organization_id),
        ).fetchone()
        if not row:
            raise NotFoundError("purchase not found")
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
            WHERE s.id = ? AND s.organization_id = ?
            """,
            (source_id, organization_id),
        ).fetchone()
        if not row:
            raise NotFoundError("sale not found")
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


def get_product(conn: db.Connection, organization_id: int, product_id: int) -> dict[str, Any]:
    product = conn.execute(
        "SELECT * FROM products WHERE id = ? AND organization_id = ?",
        (product_id, organization_id),
    ).fetchone()
    if not product:
        # 別テナントの id でも「存在しない」と同じ 404 にして有無を漏らさない（IDOR対策）。
        raise NotFoundError("product not found")
    return product


def dashboard(conn: db.Connection, organization_id: int) -> dict[str, Any]:
    products = list_products(conn, organization_id)
    total_stock_value = sum(product["stock_value"] for product in products)
    recent_movements = conn.execute(
        """
        SELECT im.*, p.sku, p.product_name
        FROM inventory_movements im
        JOIN products p ON p.id = im.product_id
        WHERE im.organization_id = ?
        ORDER BY im.created_at DESC, im.id DESC
        LIMIT 10
        """,
        (organization_id,),
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
        WHERE pu.organization_id = ? AND pu.transaction_date BETWEEN ? AND ?
        GROUP BY p.id, p.sku, p.product_name
        ORDER BY p.id
        """,
        (organization_id, month_start, today_text),
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
        WHERE s.organization_id = ? AND s.transaction_date BETWEEN ? AND ?
        GROUP BY p.id, p.sku, p.product_name
        ORDER BY p.id
        """,
        (organization_id, month_start, today_text),
    ).fetchall()
    monthly_purchase_total = conn.execute(
        """
        SELECT COALESCE(SUM(quantity * unit_price), 0) AS total
        FROM purchases
        WHERE organization_id = ? AND transaction_date BETWEEN ? AND ?
        """,
        (organization_id, month_start, today_text),
    ).fetchone()["total"]
    monthly_sales_total = conn.execute(
        """
        SELECT COALESCE(SUM(quantity * unit_price), 0) AS total
        FROM sales
        WHERE organization_id = ? AND transaction_date BETWEEN ? AND ?
        """,
        (organization_id, month_start, today_text),
    ).fetchone()["total"]
    forecast = forecast_simulation(conn, organization_id, 30)
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


def _ranked_models(conn: db.Connection, organization_id: int) -> list[str]:
    """精度(MAE)昇順のモデル名リスト。バックテスト結果が無ければ既定の優先順を補完する。
    「最良モデル」を選ぶための順位（A-6: 必要在庫を AI 予測ベースに一本化）。"""
    ranked = [
        row["model_name"]
        for row in conn.execute(
            "SELECT model_name FROM model_evaluations WHERE organization_id = ? ORDER BY mae ASC",
            (organization_id,),
        ).fetchall()
    ]
    for preferred in ("lightgbm", "sarima", "baseline"):
        if preferred not in ranked:
            ranked.append(preferred)
    return ranked


def _ai_daily_forecast(
    conn: db.Connection, organization_id: int, product_id: int, ranked_models: list[str]
) -> tuple[str, list[tuple[str, float]]]:
    """この商品の「最良モデル」の日次予測を (model_name, [(date_iso, predicted), ...]) で返す。
    予測がまだ生成されていなければ ("", [])（呼び出し側は簡易計算にフォールバックする）。"""
    present = {
        row["model_name"]
        for row in conn.execute(
            "SELECT DISTINCT model_name FROM forecasts WHERE organization_id = ? AND product_id = ?",
            (organization_id, product_id),
        ).fetchall()
    }
    model = next((m for m in ranked_models if m in present), None)
    if not model:
        return "", []
    rows = conn.execute(
        """
        SELECT target_date, predicted_quantity
        FROM forecasts
        WHERE organization_id = ? AND product_id = ? AND model_name = ?
        ORDER BY target_date
        """,
        (organization_id, product_id, model),
    ).fetchall()
    return model, [(r["target_date"], max(float(r["predicted_quantity"]), 0.0)) for r in rows]


def forecast_simulation(conn: db.Connection, organization_id: int, horizon_days: int = 30) -> dict[str, Any]:
    if horizon_days not in {30, 60, 90}:
        horizon_days = 30

    today = date.today()
    start_date = (today - timedelta(days=horizon_days - 1)).isoformat()
    end_date = today.isoformat()
    month_end = date(today.year, today.month, monthrange(today.year, today.month)[1])
    days_to_month_end = max((month_end - today).days + 1, 0)
    products = list_products(conn, organization_id)
    ranked_models = _ranked_models(conn, organization_id)
    rows = []

    for product in products:
        recent_sales_quantity = active_sales_quantity(conn, organization_id, product["id"], start_date, end_date)
        total_sales_quantity = active_sales_quantity(conn, organization_id, product["id"], "1900-01-01", end_date)
        daily_average = recent_sales_quantity / horizon_days
        seasonal_factor = monthly_seasonal_factor(conn, organization_id, product["id"], today.month)
        adjusted_daily_average = daily_average * seasonal_factor
        month_end_forecast = math.ceil(adjusted_daily_average * days_to_month_end)
        lead_time_demand = math.ceil(adjusted_daily_average * int(product["lead_time_days"]))
        # A-6: AI予測(最良モデル)があれば、リードタイム需要・月末予測販売数をそれで上書きする
        #（=必要在庫/発注量をAI予測ベースに一本化。簡易計算は予測未生成時のフォールバック）。
        ai_model, ai_daily = _ai_daily_forecast(conn, organization_id, product["id"], ranked_models)
        if ai_daily:
            lead_time_demand = math.ceil(sum(v for _, v in ai_daily[: int(product["lead_time_days"])]))
            month_end_forecast = math.ceil(sum(v for d, v in ai_daily if d <= month_end.isoformat()))
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
                "model": ai_model,  # A-6: 採用したAIモデル名（"" のときは簡易計算フォールバック）
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


def active_sales_quantity(conn: db.Connection, organization_id: int, product_id: int, start_date: str, end_date: str) -> int:
    row = conn.execute(
        """
        SELECT COALESCE(SUM(s.quantity), 0) AS quantity
        FROM sales s
        JOIN inventory_movements im ON im.source_type = 'sale' AND im.source_id = s.id
        LEFT JOIN inventory_corrections c ON c.original_movement_id = im.id
        WHERE s.organization_id = ?
          AND s.product_id = ?
          AND s.transaction_date BETWEEN ? AND ?
          AND c.id IS NULL
        """,
        (organization_id, product_id, start_date, end_date),
    ).fetchone()
    return int(row["quantity"] or 0)


def monthly_seasonal_factor(conn: db.Connection, organization_id: int, product_id: int, target_month: int) -> float:
    # 月の抽出は方言差があるため db.month_expr で吸収（strftime ⇄ EXTRACT）。
    month_sql = db.month_expr(conn, "s.transaction_date")
    rows = conn.execute(
        f"""
        SELECT {month_sql} AS month,
               SUM(s.quantity) AS quantity
        FROM sales s
        JOIN inventory_movements im ON im.source_type = 'sale' AND im.source_id = s.id
        LEFT JOIN inventory_corrections c ON c.original_movement_id = im.id
        WHERE s.organization_id = ?
          AND s.product_id = ?
          AND c.id IS NULL
        GROUP BY {month_sql}
        """,
        (organization_id, product_id),
    ).fetchall()
    if len(rows) < 6:
        return 1.0
    quantities = [float(row["quantity"] or 0) for row in rows]
    average = sum(quantities) / len(quantities)
    if average <= 0:
        return 1.0
    target = next((float(row["quantity"] or 0) for row in rows if int(row["month"]) == target_month), average)
    return min(max(target / average, 0.75), 1.4)


def product_ledger(conn: db.Connection, organization_id: int, product_id: int) -> dict[str, Any]:
    product = get_product(conn, organization_id, product_id)
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
        WHERE im.organization_id = ? AND im.product_id = ?
        ORDER BY im.movement_date ASC, im.id ASC
        """,
        (organization_id, product_id),
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


# ---------------------------------------------------------------------------
# 需要予測レベル2（A-4）の読み取り。書き込み（バッチ）は forecasting.service に委譲。
# ---------------------------------------------------------------------------
ACTUAL_CHART_DAYS = 180  # チャートに出す実績の日数（直近のみ＝見やすさ優先）。


def _actual_daily_series(conn: db.Connection, organization_id: int, product_id: int) -> list[dict[str, Any]]:
    """商品の日次実績需要（取消除外）。forecasting.data と同じ条件の読み取り。"""
    rows = conn.execute(
        """
        SELECT s.transaction_date AS date, COALESCE(SUM(s.quantity), 0) AS qty
        FROM sales s
        JOIN inventory_movements im ON im.source_type = 'sale' AND im.source_id = s.id
        LEFT JOIN inventory_corrections c ON c.original_movement_id = im.id
        WHERE s.organization_id = ?
          AND s.product_id = ?
          AND c.id IS NULL
        GROUP BY s.transaction_date
        ORDER BY s.transaction_date
        """,
        (organization_id, product_id),
    ).fetchall()
    for row in rows:
        row["qty"] = int(row["qty"] or 0)
    return rows


def forecast_series(conn: db.Connection, organization_id: int, product_id: int, model_name: str = "") -> dict[str, Any]:
    """実績線＋予測線（predicted/lower/upper）を返す。別テナントの product_id は 404。"""
    product = get_product(conn, organization_id, product_id)  # IDOR: 他テナントは NotFoundError→404

    present = [
        row["model_name"]
        for row in conn.execute(
            "SELECT DISTINCT model_name FROM forecasts WHERE organization_id = ? AND product_id = ?",
            (organization_id, product_id),
        ).fetchall()
    ]
    if not model_name:
        for preferred in ("lightgbm", "sarima", "baseline"):
            if preferred in present:
                model_name = preferred
                break
        if not model_name and present:
            model_name = present[0]

    forecast_rows = conn.execute(
        """
        SELECT target_date AS date, predicted_quantity AS predicted, lower, upper
        FROM forecasts
        WHERE organization_id = ? AND product_id = ? AND model_name = ?
        ORDER BY target_date
        """,
        (organization_id, product_id, model_name),
    ).fetchall()

    actual = _actual_daily_series(conn, organization_id, product_id)
    return {
        "product": {"id": product["id"], "sku": product["sku"], "product_name": product["product_name"]},
        "model_name": model_name,
        "available_models": present,
        "actual": actual[-ACTUAL_CHART_DAYS:],
        "forecast": forecast_rows,
    }


def list_model_evaluations(conn: db.Connection, organization_id: int) -> list[dict[str, Any]]:
    """モデル比較表（MAE/MAPE）。バックテスト結果。"""
    return conn.execute(
        """
        SELECT model_name, period, mae, mape, created_at
        FROM model_evaluations
        WHERE organization_id = ?
        ORDER BY mae ASC
        """,
        (organization_id,),
    ).fetchall()


def list_order_candidates(conn: db.Connection, organization_id: int) -> list[dict[str, Any]]:
    """発注候補（今すぐ発注が必要な商品）。適正在庫シミュレーション(AI予測ベース)から
    「今すぐ発注量 > 0」の商品を、現在在庫・必要在庫・今すぐ発注量で返す（A-6: 全画面で表記統一）。"""
    sim = forecast_simulation(conn, organization_id, 30)
    candidates = [
        {
            "sku": row["sku"],
            "product_name": row["product_name"],
            "stock_quantity": row["stock_quantity"],
            "required_inventory": row["required_inventory"],
            "recommended_order_quantity": row["recommended_order_quantity"],
        }
        for row in sim["rows"]
        if row["recommended_order_quantity"] > 0
    ]
    candidates.sort(key=lambda r: r["recommended_order_quantity"], reverse=True)
    return candidates


# ---------------------------------------------------------------------------
# A-5 経費キャプチャ（AI証憑入力）
# ---------------------------------------------------------------------------
# 鉄則（EVOLUTION_PLAN.md）: AIは画像→下書き(ai_extracted_json)まで。
# 「登録」は人が register_voucher で行い user_corrected_json を残す（自動登録はしない）。
# 解析(ai_capture)は副作用なし。DB書き込み・テナント絞り込み・監査はこの app.py が単一の主体。


def _safe_filename(name: str) -> str:
    """元ファイル名を保存用に無害化（パス区切りを除去。空なら voucher）。"""
    base = os.path.basename(name or "").strip().replace("\\", "").replace("/", "")
    return base or "voucher"


def store_voucher_image(organization_id: int, file_name: str, data: bytes) -> str:
    """元画像を保存し、保存先からの相対パス(key)を返す（DBにはこれだけ持つ）。

    保存先は organization 配下に分け、内容ハッシュをファイル名に含めてテナント越えを避ける。
    実際の置き場所（ローカル / R2 等）は storage.py が env を見て決める（A-6）。
    """
    digest = hashlib.sha256(data).hexdigest()[:16]
    rel = f"{organization_id}/{digest}_{_safe_filename(file_name)}"
    storage.save_bytes(VOUCHER_DIR, rel, data)
    return rel


def create_voucher(
    conn: db.Connection,
    organization_id: int,
    *,
    file_name: str,
    mime_type: str,
    image_bytes: bytes,
    draft: dict[str, Any],
) -> int:
    """証憑を保存する（AI下書きのみ。user_corrected_json は空＝未登録のまま）。"""
    storage_path = store_voucher_image(organization_id, file_name, image_bytes)
    return db.insert_returning_id(
        conn,
        """
        INSERT INTO vouchers
            (organization_id, file_name, storage_path, mime_type, ai_extracted_json, confidence)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            organization_id,
            _safe_filename(file_name),
            storage_path,
            mime_type,
            json.dumps(draft, ensure_ascii=False),
            float(draft.get("overall_confidence", 0) or 0),
        ),
    )


def _voucher_row(conn: db.Connection, organization_id: int, voucher_id: int) -> dict[str, Any]:
    """自テナントの証憑のみ取得。別テナント/不存在は 404（存在の有無を漏らさない＝IDOR対策）。"""
    row = conn.execute(
        "SELECT * FROM vouchers WHERE id = ? AND organization_id = ?",
        (voucher_id, organization_id),
    ).fetchone()
    if not row:
        raise NotFoundError("voucher not found")
    return row


def _voucher_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    ai = json.loads(row["ai_extracted_json"] or "{}")
    corrected = json.loads(row["user_corrected_json"]) if row["user_corrected_json"] else None
    fields = ai.get("fields", {})
    kind = ai.get("kind", "")  # purchase/sale=請求書, 空=一般経費
    if kind in ("purchase", "sale"):
        amount = float(fields.get("quantity") or 0) * float(fields.get("unit_price") or 0)
    else:
        amount = float(fields.get("amount") or 0)
    return {
        "id": row["id"],
        "file_name": row["file_name"],
        "mime_type": row["mime_type"],
        "confidence": row["confidence"],
        "kind": kind,
        "partner_name": fields.get("partner_name", ""),
        "amount": amount,
        # 仕入/売上に紐付くと user_corrected_json が入る＝「取込済み」。それが唯一の根拠。
        "registered": corrected is not None,
        "linked_source_type": (corrected or {}).get("source_type"),
        "linked_source_id": (corrected or {}).get("source_id"),
        "created_at": row["created_at"],
        "ai_extracted": ai,
        "user_corrected": corrected,
        "low_confidence_fields": ai.get("low_confidence_fields", []),
        "image_url": f"/api/vouchers/{row['id']}/image",
    }


def list_vouchers(conn: db.Connection, organization_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM vouchers WHERE organization_id = ? ORDER BY id DESC",
        (organization_id,),
    ).fetchall()
    return [_voucher_to_dict(row) for row in rows]


def voucher_detail(conn: db.Connection, organization_id: int, voucher_id: int) -> dict[str, Any]:
    return _voucher_to_dict(_voucher_row(conn, organization_id, voucher_id))


def _products_for_matching(conn: db.Connection, organization_id: int) -> list[dict[str, Any]]:
    """請求書の商品推測・単価生成に使う最小の商品マスタ。"""
    return conn.execute(
        """
        SELECT id, sku, product_name, supplier_name, purchase_unit_price, sales_unit_price
        FROM products
        WHERE organization_id = ? AND is_active = 1
        ORDER BY id
        """,
        (organization_id,),
    ).fetchall()


def capture_invoice(
    conn: db.Connection,
    organization_id: int,
    *,
    kind: str,
    file_name: str,
    mime_type: str,
    image_bytes: bytes,
    api_key: str = "",
) -> dict[str, Any]:
    """仕入/売上の請求書画像 → AI下書き（登録しない）。画像と抽出結果を証憑として保存する。

    AI が推測した商品SKUを既存マスタの product_id に解決して返す（フォームの商品選択に使う）。
    api_key: 利用者が都度渡す Anthropic キー（BYO-key）。サーバには保存・記録しない。
    """
    products = [dict(p) for p in _products_for_matching(conn, organization_id)]
    draft = ai_capture.analyze_invoice(image_bytes, mime_type, kind=kind, products=products, api_key=api_key)
    sku = str(draft["fields"].get("product_sku") or "")
    matched = next((p for p in products if str(p.get("sku")) == sku), None) if sku else None
    matched_product_id = matched["id"] if matched else None

    voucher_id = create_voucher(
        conn, organization_id, file_name=file_name, mime_type=mime_type, image_bytes=image_bytes, draft=draft
    )
    return {
        "voucher_id": voucher_id,
        "kind": kind,
        "draft": draft["fields"],
        "matched_product_id": matched_product_id,
        "confidence": draft["confidence"],
        "overall_confidence": draft["overall_confidence"],
        "low_confidence_fields": draft["low_confidence_fields"],
        "source": draft["source"],
    }


def link_voucher_to_source(
    conn: db.Connection,
    organization_id: int,
    voucher_id: int,
    source_type: str,
    source_id: int,
    registered_fields: dict[str, Any] | None = None,
) -> None:
    """人が登録した仕入/売上に証憑を紐付ける（取込済みの印＝user_corrected_json）。別テナントは無視（404相当）。"""
    row = conn.execute(
        "SELECT id FROM vouchers WHERE id = ? AND organization_id = ?",
        (voucher_id, organization_id),
    ).fetchone()
    if not row:
        # 別テナント/存在しない voucher_id は黙ってスキップ（登録自体は成立させる）。
        return
    payload = {"source_type": source_type, "source_id": source_id, "fields": registered_fields or {}}
    conn.execute(
        "UPDATE vouchers SET user_corrected_json = ? WHERE id = ? AND organization_id = ?",
        (json.dumps(payload, ensure_ascii=False), voucher_id, organization_id),
    )


def delete_voucher(conn: db.Connection, organization_id: int, voucher_id: int) -> dict[str, Any]:
    """証憑を削除する（DB行＋保存画像）。別テナント/不存在は 404（IDOR対策）。"""
    row = _voucher_row(conn, organization_id, voucher_id)
    storage.delete(VOUCHER_DIR, row["storage_path"])  # 画像が消せなくても DB 行の削除は進める
    conn.execute("DELETE FROM vouchers WHERE id = ? AND organization_id = ?", (voucher_id, organization_id))
    return {"ok": True, "deleted_voucher_id": voucher_id}


def load_voucher_image(conn: db.Connection, organization_id: int, voucher_id: int) -> tuple[bytes, str]:
    """証憑の元画像バイト列と MIME を返す。別テナント/不存在は 404（IDOR対策）。"""
    row = _voucher_row(conn, organization_id, voucher_id)
    data = storage.read_bytes(VOUCHER_DIR, row["storage_path"])
    if data is None:
        raise NotFoundError("voucher image not found")
    return data, (row["mime_type"] or "application/octet-stream")


def cancel_inventory_movement(conn: db.Connection, organization_id: int, data: dict[str, Any]) -> dict[str, Any]:
    movement_id = to_int(data.get("movement_id"))
    reason = required_text(data.get("reason") or "入力ミスのため取消", "reason")
    original = conn.execute(
        "SELECT * FROM inventory_movements WHERE id = ? AND organization_id = ?",
        (movement_id, organization_id),
    ).fetchone()
    if not original:
        # 別テナントの movement_id でも 404（存在の有無を漏らさない）。
        raise NotFoundError("movement not found")
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
        current_stock = stock_by_product(conn, organization_id).get(original["product_id"], 0)
        if current_stock + reversal_delta < 0:
            raise ValueError("取消すると在庫がマイナスになるため、先に関連する売上や調整を確認してください。")

    movement_type = "purchase_cancel" if original["source_type"] == "purchase" else "sale_cancel"
    correction_movement_id = db.insert_returning_id(
        conn,
        """
        INSERT INTO inventory_movements (
            organization_id, product_id, movement_type, source_type, source_id, movement_date,
            quantity_delta, unit_price, note
        )
        VALUES (?, ?, ?, 'correction', ?, ?, ?, ?, ?)
        """,
        (
            organization_id,
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
        INSERT INTO inventory_corrections (organization_id, original_movement_id, correction_movement_id, reason)
        VALUES (?, ?, ?, ?)
        """,
        (organization_id, movement_id, correction_movement_id, reason),
    )
    # 取消した仕訳は freee 送信待ちに残してはいけない。'cancelled' にして送信待ちから外す
    # （'failed' だと「再送」対象として残ってしまうため）。送信済み(sent)は freee 側に
    # 既に登録済みで取り消せないので触らない（取消は元帳の訂正行で記録される）。
    conn.execute(
        """
        UPDATE freee_sync_queue
        SET status = 'cancelled',
            sync_error_message = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE organization_id = ? AND source_type = ? AND source_id = ? AND status != 'sent'
        """,
        (f"在庫元帳で取消済み: {reason}", organization_id, original["source_type"], original["source_id"]),
    )
    return {"ok": True, "correction_movement_id": correction_movement_id}


def list_queue(conn: db.Connection, organization_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT q.*
        FROM freee_sync_queue q
        WHERE q.organization_id = ? AND q.status != 'cancelled'
        ORDER BY q.created_at DESC, q.id DESC
        """,
        (organization_id,),
    ).fetchall()
    for row in rows:
        row["payload"] = json.loads(row["payload_json"])
    return rows


def mark_queue_status(conn: db.Connection, organization_id: int, data: dict[str, Any]) -> dict[str, Any]:
    queue_id = to_int(data.get("id"))
    status = data.get("status", "")
    if status not in {"pending", "sent", "failed", "retry", "cancelled"}:
        raise ValueError("invalid status")
    updated = conn.execute(
        """
        UPDATE freee_sync_queue
        SET status = ?, external_accounting_id = ?, sync_error_message = ?, updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND organization_id = ?
        """,
        (status, data.get("external_accounting_id", ""), data.get("sync_error_message", ""), queue_id, organization_id),
    )
    if not _rowcount_positive(updated):
        raise NotFoundError("queue not found")
    return {"ok": True}


def fail_queue_send(conn: db.Connection, organization_id: int, queue_id: int, message: str) -> None:
    conn.execute(
        """
        UPDATE freee_sync_queue
        SET status = 'failed',
            sync_error_message = ?,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND organization_id = ?
        """,
        (message, queue_id, organization_id),
    )


def send_queue_to_pseudo_freee(conn: db.Connection, organization_id: int, data: dict[str, Any]) -> dict[str, Any]:
    queue_id = to_int(data.get("id"))
    queue = conn.execute(
        "SELECT * FROM freee_sync_queue WHERE id = ? AND organization_id = ?",
        (queue_id, organization_id),
    ).fetchone()
    if not queue:
        # 別テナントのキュー id でも 404。
        raise NotFoundError("queue not found")
    if queue["status"] == "sent":
        raise ValueError("送信済みキューは再送できません")
    if queue["status"] == "cancelled":
        raise ValueError("取消済みの仕訳は freee へ送信できません")

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
        fail_queue_send(conn, organization_id, queue_id, message)
        raise ValueError(message) from exc
    except urllib.error.URLError as exc:
        message = f"疑似freeeに接続できません: {exc.reason}"
        fail_queue_send(conn, organization_id, queue_id, message)
        raise ValueError(message) from exc
    except TimeoutError as exc:
        message = "疑似freee送信がタイムアウトしました"
        fail_queue_send(conn, organization_id, queue_id, message)
        raise ValueError(message) from exc

    if not response_data.get("ok"):
        message = str(response_data.get("error") or "疑似freee送信に失敗しました")
        fail_queue_send(conn, organization_id, queue_id, message)
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
        WHERE id = ? AND organization_id = ?
        """,
        (external_accounting_id, queue_id, organization_id),
    )
    return {
        "ok": True,
        "pseudo_freee_deal_id": pseudo_freee_deal_id,
        "external_accounting_id": external_accounting_id,
        "duplicate": bool(response_data.get("duplicate")),
    }


def _rowcount_positive(cursor: Any) -> bool:
    """UPDATE/DELETE が 1 行以上に当たったか（sqlite3 / psycopg どちらも rowcount を持つ）。"""
    try:
        return int(cursor.rowcount) > 0
    except Exception:
        return True


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


@app.exception_handler(auth.AuthError)
async def handle_auth_error(request: Request, exc: auth.AuthError) -> JSONResponse:
    # 未認証/トークン不正は 401（検証プラン 1: 未認証で各 API に到達不可）。
    return JSONResponse(status_code=401, content={"error": str(exc) or "認証が必要です"})


@app.exception_handler(ForbiddenError)
async def handle_forbidden_error(request: Request, exc: ForbiddenError) -> JSONResponse:
    # 認証済みだが権限不足は 403（検証プラン 3: viewer は更新系不可）。
    return JSONResponse(status_code=403, content={"error": str(exc)})


@app.exception_handler(NotFoundError)
async def handle_not_found_error(request: Request, exc: NotFoundError) -> JSONResponse:
    # 別テナント/存在しない id は 404（検証プラン 2: IDOR）。
    return JSONResponse(status_code=404, content={"error": str(exc) or "not found"})


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


# --- 認証/認可（A-3）-------------------------------------------------------
# 全 API は current_identity を依存に持ち、未認証なら AuthError→401。
# 更新系は require_roles("admin","staff") で viewer を弾く（403）。認可は常にサーバ側。
def current_identity(
    authorization: str | None = Header(default=None),
    x_dev_user_id: str | None = Header(default=None, alias="X-Dev-User-Id"),
) -> Identity:
    token = auth.bearer_token_from_header(authorization)
    if token is None:
        # dev モード（Clerk 未設定のローカル/テスト）に限り、トークン無しを許可する。
        # X-Dev-User-Id を変えると別テナントとして振る舞うのでテナント分離テストに使える。
        if auth.auth_dev_mode():
            user_id = (x_dev_user_id or "dev-user").strip() or "dev-user"
            return resolve_identity(user_id)
        raise auth.AuthError("認証が必要です（Authorization: Bearer トークンがありません）")
    claims = auth.verify_token(token)  # 失敗時 AuthError→401
    return resolve_identity(str(claims["sub"]))


def require_roles(*roles: str) -> Any:
    """指定ロールのみ通す依存を返す（RBAC）。"""

    def dependency(identity: Identity = Depends(current_identity)) -> Identity:
        if identity.role not in roles:
            raise ForbiddenError(
                f"この操作には {'/'.join(roles)} 権限が必要です（現在のロール: {identity.role}）"
            )
        return identity

    return dependency


# 更新系で使い回す依存（admin と staff は書き込み可、viewer は読み取り専用）。
WRITER = Depends(require_roles("admin", "staff"))


# --- 画面 -------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index() -> str:
    # 公開キー（ブラウザに出してよい）と dev フラグを埋め込んで配信する。
    return render_index(
        publishable_key=auth.clerk_publishable_key(),
        clerk_configured=auth.clerk_configured(),
        dev_mode=auth.auth_dev_mode(),
    )


# --- 参照系 API（認証必須・全ロール可）-------------------------------------
@app.get("/api/dashboard")
def api_dashboard(identity: Identity = Depends(current_identity)) -> dict[str, Any]:
    with get_conn() as conn:
        return dashboard(conn, identity.organization_id)


@app.get("/api/products")
def api_products(identity: Identity = Depends(current_identity)) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return list_products(conn, identity.organization_id)


@app.get("/api/business-partners")
def api_business_partners(identity: Identity = Depends(current_identity)) -> dict[str, Any]:
    with get_conn() as conn:
        return list_business_partners(conn, identity.organization_id)


@app.get("/api/forecast-simulation")
def api_forecast_simulation(horizon_days: int = 30, identity: Identity = Depends(current_identity)) -> dict[str, Any]:
    with get_conn() as conn:
        return forecast_simulation(conn, identity.organization_id, horizon_days)


# --- 需要予測レベル2（A-4）------------------------------------------------
@app.get("/api/forecast/models")
def api_forecast_models(identity: Identity = Depends(current_identity)) -> dict[str, Any]:
    # この環境で利用可能なモデル（依存が無いものは自動的に外れる）。
    from forecasting import models as forecast_models

    return {
        "models": [
            {"name": model.name, "label": model.label}
            for model in forecast_models.available_models()
        ]
    }


@app.get("/api/forecast/series")
def api_forecast_series(
    product_id: int, model_name: str = "", identity: Identity = Depends(current_identity)
) -> dict[str, Any]:
    with get_conn() as conn:
        return forecast_series(conn, identity.organization_id, product_id, model_name)


@app.get("/api/forecast/evaluations")
def api_forecast_evaluations(identity: Identity = Depends(current_identity)) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return list_model_evaluations(conn, identity.organization_id)


@app.get("/api/forecast/order-candidates")
def api_forecast_order_candidates(identity: Identity = Depends(current_identity)) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return list_order_candidates(conn, identity.organization_id)


# --- 証憑（仕入・売上請求書）参照系（A-5・全ロール可）-----------------------
@app.get("/api/vouchers")
def api_vouchers(identity: Identity = Depends(current_identity)) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return list_vouchers(conn, identity.organization_id)


@app.get("/api/vouchers/{voucher_id}")
def api_voucher_detail(voucher_id: int, identity: Identity = Depends(current_identity)) -> dict[str, Any]:
    with get_conn() as conn:
        return voucher_detail(conn, identity.organization_id, voucher_id)


@app.get("/api/vouchers/{voucher_id}/image")
def api_voucher_image(voucher_id: int, identity: Identity = Depends(current_identity)) -> Response:
    # 元画像の配信もテナント絞り込み（別テナントの id は 404＝IDOR対策）。
    with get_conn() as conn:
        data, media_type = load_voucher_image(conn, identity.organization_id, voucher_id)
    return Response(content=data, media_type=media_type)


@app.delete("/api/vouchers/{voucher_id}")
def api_delete_voucher(voucher_id: int, identity: Identity = WRITER) -> dict[str, Any]:
    # 証憑の削除は admin/staff のみ。別テナントの id は 404。
    with get_conn() as conn:
        result = delete_voucher(conn, identity.organization_id, voucher_id)
        record_audit(conn, identity.organization_id, identity.user_id, "voucher.delete", "voucher", voucher_id)
        return result


@app.get("/api/products/{product_id}/ledger")
def api_product_ledger(product_id: int, identity: Identity = Depends(current_identity)) -> dict[str, Any]:
    with get_conn() as conn:
        return product_ledger(conn, identity.organization_id, product_id)


@app.get("/api/freee-sync-queue")
def api_freee_sync_queue(identity: Identity = Depends(current_identity)) -> list[dict[str, Any]]:
    with get_conn() as conn:
        return list_queue(conn, identity.organization_id)


@app.get("/api/freee-preview")
def api_freee_preview(source_type: str = "", source_id: int = 0, identity: Identity = Depends(current_identity)) -> dict[str, Any]:
    with get_conn() as conn:
        return build_freee_payload(conn, identity.organization_id, source_type, source_id)


@app.get("/api/audit-logs")
def api_audit_logs(identity: Identity = Depends(require_roles("admin"))) -> list[dict[str, Any]]:
    # 監査ログの閲覧は admin のみ（「見せる機能」）。
    with get_conn() as conn:
        return list_audit_logs(conn, identity.organization_id)


# --- 更新系 API（成功時 201 Created・viewer は 403）-------------------------
@app.post("/api/forecast/run")
def api_forecast_run(horizon_days: int = 30, identity: Identity = WRITER) -> dict[str, Any]:
    # 予測バッチの実行は admin/staff のみ（重い処理＋データ更新のため）。
    from forecasting import service as forecast_service

    with get_conn() as conn:
        return forecast_service.run_forecast(
            conn, identity.organization_id, horizon_days=horizon_days, actor_user_id=identity.user_id
        )


@app.post("/api/products", status_code=201)
def api_create_product(data: dict[str, Any] = Depends(parse_json_body), identity: Identity = WRITER) -> dict[str, Any]:
    with get_conn() as conn:
        result = create_product(conn, identity.organization_id, data)
        record_audit(conn, identity.organization_id, identity.user_id, "product.create", "product", data.get("sku", ""))
        return result


def _maybe_link_voucher(conn: db.Connection, organization_id: int, data: dict[str, Any], source_type: str, source_id: int) -> None:
    """請求書から取り込んで登録した場合、その証憑(voucher_id)を仕入/売上に紐付ける。"""
    raw = data.get("voucher_id")
    if not raw:
        return
    try:
        voucher_id = int(raw)
    except (TypeError, ValueError):
        return
    link_voucher_to_source(
        conn, organization_id, voucher_id, source_type, source_id,
        {k: data.get(k) for k in ("partner_name", "invoice_no", "transaction_date", "quantity", "unit_price", "tax_rate")},
    )


@app.post("/api/purchases", status_code=201)
def api_create_purchase(data: dict[str, Any] = Depends(parse_json_body), identity: Identity = WRITER) -> dict[str, Any]:
    with get_conn() as conn:
        result = create_purchase(conn, identity.organization_id, data)
        record_audit(conn, identity.organization_id, identity.user_id, "purchase.create", "purchase", result.get("purchase_id"), {"invoice_no": data.get("invoice_no")})
        _maybe_link_voucher(conn, identity.organization_id, data, "purchase", result["purchase_id"])
        return result


@app.post("/api/sales", status_code=201)
def api_create_sale(data: dict[str, Any] = Depends(parse_json_body), identity: Identity = WRITER) -> dict[str, Any]:
    with get_conn() as conn:
        result = create_sale(conn, identity.organization_id, data)
        record_audit(conn, identity.organization_id, identity.user_id, "sale.create", "sale", result.get("sale_id"), {"invoice_no": data.get("invoice_no")})
        _maybe_link_voucher(conn, identity.organization_id, data, "sale", result["sale_id"])
        return result


@app.post("/api/business-partners", status_code=201)
def api_create_business_partner(data: dict[str, Any] = Depends(parse_json_body), identity: Identity = WRITER) -> dict[str, Any]:
    with get_conn() as conn:
        result = create_business_partner(conn, identity.organization_id, data)
        record_audit(conn, identity.organization_id, identity.user_id, "business_partner.create", "business_partner", data.get("partner_name", ""))
        return result


@app.post("/api/freee-sync-queue/send", status_code=201)
def api_send_queue(data: dict[str, Any] = Depends(parse_json_body), identity: Identity = WRITER) -> dict[str, Any]:
    with get_conn() as conn:
        result = send_queue_to_pseudo_freee(conn, identity.organization_id, data)
        record_audit(conn, identity.organization_id, identity.user_id, "freee_queue.send", "freee_sync_queue", data.get("id"), {"external_accounting_id": result.get("external_accounting_id")})
        return result


@app.post("/api/freee-sync-queue/status", status_code=201)
def api_mark_queue_status(data: dict[str, Any] = Depends(parse_json_body), identity: Identity = WRITER) -> dict[str, Any]:
    with get_conn() as conn:
        result = mark_queue_status(conn, identity.organization_id, data)
        record_audit(conn, identity.organization_id, identity.user_id, "freee_queue.status", "freee_sync_queue", data.get("id"), {"status": data.get("status")})
        return result


@app.post("/api/inventory-movements/cancel", status_code=201)
def api_cancel_movement(data: dict[str, Any] = Depends(parse_json_body), identity: Identity = WRITER) -> dict[str, Any]:
    with get_conn() as conn:
        result = cancel_inventory_movement(conn, identity.organization_id, data)
        record_audit(conn, identity.organization_id, identity.user_id, "inventory_movement.cancel", "inventory_movement", data.get("movement_id"), {"reason": data.get("reason")})
        return result


# --- 仕入・売上請求書の取り込み 更新系（A-5・admin/staff のみ・viewer は 403）---
@app.post("/api/invoice-capture", status_code=201)
async def api_invoice_capture(
    kind: str = "purchase",
    file: UploadFile = File(...),
    identity: Identity = WRITER,
    x_anthropic_key: str = Header(default="", alias="X-Anthropic-Key"),
) -> dict[str, Any]:
    # 仕入/売上の請求書画像 → AI下書き → 証憑保存。**ここでは登録しない**（登録は人が仕入/売上フォームで行う）。
    # x_anthropic_key: 利用者が貼った自分の Anthropic キー（BYO-key）。受け取って解析に使うだけで保存・記録しない。
    if kind not in {"purchase", "sale"}:
        raise ValueError("kind は purchase または sale です。")
    image_bytes = await file.read()
    if not image_bytes:
        raise ValueError("画像がありません。")
    with get_conn() as conn:
        result = capture_invoice(
            conn,
            identity.organization_id,
            kind=kind,
            file_name=file.filename or "",
            mime_type=file.content_type or "",
            image_bytes=image_bytes,
            api_key=x_anthropic_key,
        )
        record_audit(
            conn, identity.organization_id, identity.user_id, "voucher.capture", "voucher", result["voucher_id"],
            {"kind": kind, "source": result.get("source"), "overall_confidence": result.get("overall_confidence")},
        )
    return result


# ---------------------------------------------------------------------------
def run() -> None:
    import uvicorn

    print(f"Inventory dashboard running at http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    run()

