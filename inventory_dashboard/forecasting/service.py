"""予測バッチの本体（EVOLUTION_PLAN.md A-4）。

商品ごとに各モデルを学習→予測（forecasts）、バックテスト評価（model_evaluations）、
最良モデルで在庫が必要水準を割る日を検出して発注候補（order_candidates）を保存する。
すべて呼び出し元のトランザクション内（app の get_conn）で実行し、organization_id で絞る。
app.py には依存しない（循環回避。監査も直接 SQL で書く）。
"""

from __future__ import annotations

import json
import math
from typing import Any

from . import backtest, data
from . import models as models_mod


def run_forecast(
    conn: Any,
    organization_id: int,
    horizon_days: int = 30,
    test_days: int = 28,
    actor_user_id: str = "",
) -> dict[str, Any]:
    """組織の予測を一括再計算して各テーブルへ保存し、サマリを返す。"""
    models = models_mod.available_models()

    # 再実行時は前回分を置き換える（org 単位）。
    for table in ("forecasts", "model_evaluations", "order_candidates"):
        conn.execute(f"DELETE FROM {table} WHERE organization_id = ?", (organization_id,))

    products = data.list_active_products(conn, organization_id)
    stock_map = data.current_stock(conn, organization_id)

    forecast_rows: list[tuple] = []
    eval_acc: dict[str, dict[str, list[float]]] = {m.name: {"mae": [], "mape": []} for m in models}
    best_per_product: dict[int, tuple[str, Any, dict[str, Any]]] = {}
    forecasted = 0
    skipped: list[str] = []

    for product in products:
        product_id = product["id"]
        series = data.load_demand_series(conn, organization_id, product_id)
        if series.empty or float(series.sum()) <= 0 or len(series) < backtest.MIN_TRAIN_DAYS:
            skipped.append(product["sku"])
            continue

        factor_pivot = data.load_factor_pivot(conn, organization_id, product_id)
        product_forecasts: dict[str, Any] = {}
        product_scores: dict[str, float] = {}

        for model in models:
            try:
                forecast = model.forecast(series, factor_pivot, horizon_days)
            except Exception:
                # 個別モデルの失敗は致命にしない（他モデル・他商品は続行）。
                continue
            product_forecasts[model.name] = forecast
            for day, row in forecast.iterrows():
                forecast_rows.append(
                    (
                        organization_id,
                        product_id,
                        day.date().isoformat(),
                        model.name,
                        float(row["predicted"]),
                        float(row["lower"]),
                        float(row["upper"]),
                    )
                )
            try:
                evaluation = backtest.backtest_model(model, series, factor_pivot, test_days)
            except Exception:
                evaluation = None
            if evaluation:
                eval_acc[model.name]["mae"].append(evaluation["mae"])
                if not math.isnan(evaluation["mape"]):
                    eval_acc[model.name]["mape"].append(evaluation["mape"])
                product_scores[model.name] = evaluation["mae"]

        if not product_forecasts:
            skipped.append(product["sku"])
            continue
        forecasted += 1

        # 商品ごとの最良モデル（バックテスト MAE 最小。無ければ baseline 優先）。
        if product_scores:
            best_name = min(product_scores, key=product_scores.get)
        elif "baseline" in product_forecasts:
            best_name = "baseline"
        else:
            best_name = next(iter(product_forecasts))
        best_per_product[product_id] = (best_name, product_forecasts[best_name], product)

    if forecast_rows:
        conn.executemany(
            """
            INSERT INTO forecasts
                (organization_id, product_id, target_date, model_name, predicted_quantity, lower, upper)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            forecast_rows,
        )

    # 精度指標はモデル/期間単位で集計（商品横断の平均）。
    period = f"直近{test_days}日ホールドアウト"
    eval_rows: list[tuple] = []
    for name, acc in eval_acc.items():
        if not acc["mae"]:
            continue
        mae = sum(acc["mae"]) / len(acc["mae"])
        mape = sum(acc["mape"]) / len(acc["mape"]) if acc["mape"] else 0.0
        eval_rows.append((organization_id, name, period, float(mae), float(mape)))
    if eval_rows:
        conn.executemany(
            "INSERT INTO model_evaluations (organization_id, model_name, period, mae, mape) VALUES (?, ?, ?, ?, ?)",
            eval_rows,
        )

    # 最良モデルの予測で在庫を取り崩し、必要水準を割る最初の日を発注候補にする。
    candidate_rows: list[tuple] = []
    for product_id, (best_name, forecast, product) in best_per_product.items():
        reorder_level = int(product["reorder_point"]) + int(product["safety_stock"])
        projected = float(stock_map.get(product_id, 0))
        for day, row in forecast.iterrows():
            projected -= float(row["predicted"])
            if projected < reorder_level:
                recommended = max(
                    int(math.ceil(reorder_level - projected)),
                    int(product["min_order_quantity"]),
                )
                basis = f"{best_name}予測: {day.date().isoformat()} に在庫が必要水準({reorder_level})を割る見込み"
                candidate_rows.append(
                    (organization_id, product_id, day.date().isoformat(), recommended, basis, "open")
                )
                break
    if candidate_rows:
        conn.executemany(
            """
            INSERT INTO order_candidates
                (organization_id, product_id, suggested_date, recommended_quantity, basis, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            candidate_rows,
        )

    aggregated = {name: sum(acc["mae"]) / len(acc["mae"]) for name, acc in eval_acc.items() if acc["mae"]}
    best_overall = min(aggregated, key=aggregated.get) if aggregated else None

    summary = {
        "models": [m.name for m in models],
        "products_forecasted": forecasted,
        "products_skipped": skipped,
        "horizon_days": horizon_days,
        "forecast_rows": len(forecast_rows),
        "order_candidates": len(candidate_rows),
        "best_model": best_overall,
        "evaluations": [
            {"model_name": row[1], "mae": round(row[3], 3), "mape": round(row[4], 2)}
            for row in eval_rows
        ],
    }

    # 監査ログ（誰が予測を回したか）。同一トランザクションに残す。
    conn.execute(
        """
        INSERT INTO audit_logs (organization_id, actor_user_id, action, target_type, target_id, detail_json)
        VALUES (?, ?, 'forecast.run', 'forecast', '', ?)
        """,
        (organization_id, actor_user_id or "", json.dumps(summary, ensure_ascii=False)),
    )
    return summary
