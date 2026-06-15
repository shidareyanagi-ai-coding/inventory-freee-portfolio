"""LightGBM 用の特徴量エンジニアリング（EVOLUTION_PLAN.md A-4）。

「カレンダー」「ラグ」「移動統計」「外部要因」を作る。これらの交互作用を学習できることが
LightGBM が baseline（移動平均×季節）を上回れる理由＝レベル2 の肝。
特徴量名はすべて ASCII（LightGBM が特殊文字を嫌うため）。
"""

from __future__ import annotations

import pandas as pd

# ラグ（自己相関）: 前日・週・隔週・月・前年同日。
LAGS = [1, 7, 14, 28, 365]
# 移動統計の窓。
ROLL_WINDOWS = [7, 28]


def calendar_features(index: pd.DatetimeIndex) -> pd.DataFrame:
    """日付から曜日・月・週・日・月初/月末フラグを作る。"""
    iso = index.isocalendar()
    return pd.DataFrame(
        {
            "dow": index.dayofweek.astype("int64"),
            "month": index.month.astype("int64"),
            "weekofyear": iso["week"].to_numpy().astype("int64"),
            "day": index.day.astype("int64"),
            "is_month_start": index.is_month_start.astype("int64"),
            "is_month_end": index.is_month_end.astype("int64"),
        },
        index=index,
    )


def align_factors(factor_pivot: pd.DataFrame, index: pd.DatetimeIndex) -> pd.DataFrame:
    """外部要因ピボットを対象 index に合わせ、欠損日は 0。列名は factor_0.. に正規化。"""
    if factor_pivot is None or factor_pivot.empty:
        return pd.DataFrame(index=index)
    aligned = factor_pivot.reindex(index, fill_value=0.0)
    aligned.columns = [f"factor_{i}" for i in range(aligned.shape[1])]
    return aligned


def lag_value(history: dict, day: pd.Timestamp, lag: int) -> float:
    """history(dict: Timestamp→値) から lag 日前の値を引く（無ければ NaN）。"""
    return history.get(day - pd.Timedelta(days=lag), float("nan"))


def rolling_stat(history: dict, day: pd.Timestamp, window: int, kind: str) -> float:
    """day の直前 window 日の平均/標準偏差（存在する日だけで計算。無ければ NaN）。"""
    values = [
        history[day - pd.Timedelta(days=k)]
        for k in range(1, window + 1)
        if (day - pd.Timedelta(days=k)) in history
    ]
    if not values:
        return float("nan")
    series = pd.Series(values, dtype="float64")
    return float(series.mean()) if kind == "mean" else float(series.std(ddof=0))


def dynamic_features(history: dict, day: pd.Timestamp) -> dict[str, float]:
    """1日分のラグ・移動統計を辞書で返す（再帰予測のループから使う）。"""
    feats: dict[str, float] = {}
    for lag in LAGS:
        feats[f"lag_{lag}"] = lag_value(history, day, lag)
    for window in ROLL_WINDOWS:
        feats[f"rollmean_{window}"] = rolling_stat(history, day, window, "mean")
    feats["rollstd_7"] = rolling_stat(history, day, 7, "std")
    return feats
