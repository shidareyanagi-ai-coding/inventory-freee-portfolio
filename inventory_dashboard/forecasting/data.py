"""予測用のデータアクセス（EVOLUTION_PLAN.md A-4）。

DB から「日次の実績需要系列」「外部要因」「商品・在庫」を取り出す。
app.py に依存しない（service が app を import すると循環するため、ここで完結させる）。
取消（inventory_corrections）された売上は需要から除外する＝既存 active_sales_quantity と同じ扱い。
"""

from __future__ import annotations

from typing import Any

import pandas as pd

import db


def load_demand_series(conn: Any, organization_id: int, product_id: int) -> pd.Series:
    """商品の日次実績需要を連続日次の pd.Series（欠損日は 0）で返す。

    空（売上ゼロ）なら空 Series。index は DatetimeIndex（freq='D'）。
    """
    rows = conn.execute(
        """
        SELECT s.transaction_date AS d, COALESCE(SUM(s.quantity), 0) AS qty
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
    if not rows:
        return pd.Series(dtype="float64")

    dates = pd.to_datetime([row["d"] for row in rows])
    values = [float(row["qty"] or 0) for row in rows]
    series = pd.Series(values, index=dates).sort_index()
    # 連続した日次インデックスに整える（取引の無い日は需要 0）。
    full_index = pd.date_range(series.index.min(), series.index.max(), freq="D")
    return series.reindex(full_index, fill_value=0.0)


def load_factor_pivot(conn: Any, organization_id: int, product_id: int) -> pd.DataFrame:
    """外部要因を「日付×factor_type（値1.0）」のピボットで返す。

    組織横断（product_id IS NULL）＋当該商品の要因をまとめる。無ければ空 DataFrame。
    """
    rows = conn.execute(
        """
        SELECT factor_date AS d, factor_type AS t, MAX(value) AS v
        FROM external_factors
        WHERE organization_id = ?
          AND (product_id IS NULL OR product_id = ?)
        GROUP BY factor_date, factor_type
        """,
        (organization_id, product_id),
    ).fetchall()
    if not rows:
        return pd.DataFrame()

    frame = pd.DataFrame(rows)
    frame["d"] = pd.to_datetime(frame["d"])
    pivot = frame.pivot_table(index="d", columns="t", values="v", aggfunc="max").fillna(0.0)
    pivot.index.name = None
    return pivot


def list_active_products(conn: Any, organization_id: int) -> list[dict[str, Any]]:
    """予測対象の商品（有効なもの）を返す。"""
    return conn.execute(
        """
        SELECT id, sku, product_name, reorder_point, safety_stock,
               lead_time_days, min_order_quantity
        FROM products
        WHERE organization_id = ? AND is_active = 1
        ORDER BY id
        """,
        (organization_id,),
    ).fetchall()


def current_stock(conn: Any, organization_id: int) -> dict[int, int]:
    """商品ごとの現在在庫（在庫移動の合計）。app.stock_by_product と同じ計算。"""
    rows = conn.execute(
        """
        SELECT product_id, COALESCE(SUM(quantity_delta), 0) AS stock
        FROM inventory_movements
        WHERE organization_id = ?
        GROUP BY product_id
        """,
        (organization_id,),
    ).fetchall()
    return {row["product_id"]: int(row["stock"]) for row in rows}
