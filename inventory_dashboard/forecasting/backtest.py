"""バックテスト（EVOLUTION_PLAN.md A-4 / 検証プラン5）。

末尾 test_days 日をホールドアウトにして、各モデルが「実際に当たるか」を MAE/MAPE で測る。
これにより「LightGBM が baseline を上回る」をダッシュボードの数字で示せる（レベル2 の見せ場）。
MAE/MAPE は日次予測ではなくモデル/期間単位で保持する（model_evaluations）。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# これより短い系列はバックテストしない（学習に最低限必要な日数）。
MIN_TRAIN_DAYS = 60


def metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    """MAE（常時）と MAPE（実績>0 の日だけ・ゼロ割回避）を返す。"""
    actual = np.asarray(actual, dtype="float64")
    predicted = np.asarray(predicted, dtype="float64")
    mae = float(np.mean(np.abs(predicted - actual)))
    nonzero = actual > 0
    if nonzero.any():
        mape = float(np.mean(np.abs(predicted[nonzero] - actual[nonzero]) / actual[nonzero]) * 100.0)
    else:
        mape = float("nan")
    return {"mae": mae, "mape": mape}


def backtest_model(model: Any, series: pd.Series, factor_pivot: pd.DataFrame, test_days: int = 28) -> dict[str, float] | None:
    """1モデルをホールドアウト評価。系列が短すぎるときは None。"""
    if len(series) < MIN_TRAIN_DAYS + test_days:
        return None

    train = series.iloc[:-test_days]
    test = series.iloc[-test_days:]

    forecast = model.forecast(train, factor_pivot, horizon=test_days)
    # 予測を実測 index に合わせる（連続日次なので一致するはず。安全のため reindex）。
    predicted = forecast["predicted"].reindex(test.index).to_numpy()
    result = metrics(test.to_numpy(), predicted)
    result["n"] = int(len(test))
    return result
