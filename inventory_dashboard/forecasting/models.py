"""予測モデル群とレジストリ（EVOLUTION_PLAN.md A-4）。

3モデルを同じインターフェース（forecast(series, factors, horizon) → DataFrame）で揃える:
  - baseline : 移動平均 × 月次季節係数（追加依存なし・常に利用可）
  - sarima   : statsmodels の SARIMAX（週次季節）。失敗時は ETS(Holt-Winters) にフォールバック
  - lightgbm : 勾配ブースティング＋分位点回帰（特徴量で非線形・複数要因を学習＝主役）

遅延 import: 依存が無いモデルは available()=False となり、レジストリから自動的に外れる。
baseline は必ず動くので「最低でも予測は出る」ことを保証する。

返り値の DataFrame: index=未来日(DatetimeIndex), columns=[predicted, lower, upper]（すべて >= 0）。
"""

from __future__ import annotations

import warnings
from typing import Any

import numpy as np
import pandas as pd

from . import features

# 80% 区間に対応する正規分布の z 値（baseline / ETS のバンド幅）。
_Z_80 = 1.2816


def _future_index(series: pd.Series, horizon: int) -> pd.DatetimeIndex:
    last = series.index.max()
    return pd.date_range(last + pd.Timedelta(days=1), periods=horizon, freq="D")


def _finalize(index: pd.DatetimeIndex, predicted, lower, upper) -> pd.DataFrame:
    """予測を 0 でクランプし lower<=predicted<=upper を保証した DataFrame に整える。"""
    df = pd.DataFrame(
        {
            "predicted": np.asarray(predicted, dtype="float64"),
            "lower": np.asarray(lower, dtype="float64"),
            "upper": np.asarray(upper, dtype="float64"),
        },
        index=index,
    ).clip(lower=0.0)
    df["lower"] = df[["lower", "predicted"]].min(axis=1)
    df["upper"] = df[["upper", "predicted"]].max(axis=1)
    return df


class BaselineModel:
    name = "baseline"
    label = "ベースライン(移動平均×季節)"

    @staticmethod
    def available() -> bool:
        return True

    def forecast(self, series: pd.Series, factor_pivot: pd.DataFrame, horizon: int) -> pd.DataFrame:
        index = _future_index(series, horizon)
        overall = float(series.mean()) if len(series) else 0.0
        recent = float(series.tail(28).mean()) if len(series) else 0.0

        # 月次季節係数（その月の平均 / 全体平均）。0.5〜1.8 にクランプ。
        by_month = series.groupby(series.index.month).mean() if len(series) else pd.Series(dtype="float64")
        season = {
            int(m): min(max(v / overall, 0.5), 1.8) if overall > 0 else 1.0
            for m, v in by_month.items()
        }

        predicted = np.array([recent * season.get(int(d.month), 1.0) for d in index])

        # 残差の標準偏差からバンド幅を作る。
        fitted = np.array([recent * season.get(int(d.month), 1.0) for d in series.index])
        resid_std = float(np.std(series.to_numpy() - fitted)) if len(series) else 0.0
        margin = _Z_80 * resid_std
        return _finalize(index, predicted, predicted - margin, predicted + margin)


class SarimaModel:
    name = "sarima"
    label = "SARIMA(古典時系列)"

    @staticmethod
    def available() -> bool:
        try:
            import statsmodels  # noqa: F401

            return True
        except Exception:
            return False

    def forecast(self, series: pd.Series, factor_pivot: pd.DataFrame, horizon: int) -> pd.DataFrame:
        from statsmodels.tsa.holtwinters import ExponentialSmoothing
        from statsmodels.tsa.statespace.sarimax import SARIMAX

        index = _future_index(series, horizon)
        observed = series.asfreq("D").fillna(0.0)

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                model = SARIMAX(
                    observed,
                    order=(1, 1, 1),
                    seasonal_order=(1, 0, 1, 7),
                    enforce_stationarity=False,
                    enforce_invertibility=False,
                )
                result = model.fit(disp=False, maxiter=50)
                forecast = result.get_forecast(steps=horizon)
                mean = np.asarray(forecast.predicted_mean)
                conf = np.asarray(forecast.conf_int(alpha=0.2))
                return _finalize(index, mean, conf[:, 0], conf[:, 1])
            except Exception:
                # SARIMAX が収束しないときは Holt-Winters(ETS) に退避する。
                ets = ExponentialSmoothing(
                    observed,
                    trend="add",
                    seasonal="add",
                    seasonal_periods=7,
                    initialization_method="estimated",
                ).fit()
                mean = np.asarray(ets.forecast(horizon))
                resid_std = float(np.std(observed.to_numpy() - np.asarray(ets.fittedvalues)))
                margin = _Z_80 * resid_std
                return _finalize(index, mean, mean - margin, mean + margin)


class LightGBMModel:
    name = "lightgbm"
    label = "LightGBM(機械学習)"

    # 分位点回帰の 3 本（中央値 + 80% 区間）。
    _QUANTILES = {"lower": 0.1, "predicted": 0.5, "upper": 0.9}

    @staticmethod
    def available() -> bool:
        try:
            import lightgbm  # noqa: F401

            return True
        except Exception:
            return False

    def _params(self, alpha: float) -> dict[str, Any]:
        return dict(
            objective="quantile",
            alpha=alpha,
            n_estimators=300,
            learning_rate=0.05,
            num_leaves=31,
            min_child_samples=20,
            subsample=1.0,
            colsample_bytree=0.9,
            random_state=42,
            deterministic=True,
            force_col_wise=True,
            verbosity=-1,
            n_jobs=1,
        )

    def forecast(self, series: pd.Series, factor_pivot: pd.DataFrame, horizon: int) -> pd.DataFrame:
        import lightgbm as lgb

        index = _future_index(series, horizon)
        full_index = series.index.append(index)

        calendar = features.calendar_features(full_index)
        factors = features.align_factors(factor_pivot, full_index)
        static = pd.concat([calendar, factors], axis=1)

        history: dict[pd.Timestamp, float] = {ts: float(v) for ts, v in series.items()}

        # 学習データ（履歴日のうち、最初の1日を除く＝ラグが作れる日）を構築。
        train_rows: list[dict[str, float]] = []
        targets: list[float] = []
        for ts in series.index[1:]:
            row = static.loc[ts].to_dict()
            row.update(features.dynamic_features(history, ts))
            train_rows.append(row)
            targets.append(history[ts])
        if not train_rows:
            raise ValueError("学習データが不足しています（履歴が短すぎます）")

        x_train = pd.DataFrame(train_rows)
        feature_columns = list(x_train.columns)
        y_train = np.asarray(targets, dtype="float64")

        fitted = {}
        for key, alpha in self._QUANTILES.items():
            estimator = lgb.LGBMRegressor(**self._params(alpha))
            estimator.fit(x_train, y_train)
            fitted[key] = estimator

        # 再帰予測: 予測した中央値を履歴へ戻しながら 1 日ずつ先へ進む。
        working = dict(history)
        preds = {"lower": [], "predicted": [], "upper": []}
        for day in index:
            row = static.loc[day].to_dict()
            row.update(features.dynamic_features(working, day))
            x_row = pd.DataFrame([row])[feature_columns]
            point = {key: float(est.predict(x_row)[0]) for key, est in fitted.items()}
            for key in preds:
                preds[key].append(point[key])
            working[day] = max(point["predicted"], 0.0)

        return _finalize(index, preds["predicted"], preds["lower"], preds["upper"])


# モデルレジストリ（順序＝表示・比較の並び）。
_ALL_MODELS = [BaselineModel, SarimaModel, LightGBMModel]


def available_models() -> list[Any]:
    """この環境で利用可能なモデルのインスタンス一覧（baseline は常に含む）。"""
    return [cls() for cls in _ALL_MODELS if cls.available()]


def available_model_names() -> list[str]:
    return [model.name for model in available_models()]


def get_model(name: str) -> Any | None:
    for cls in _ALL_MODELS:
        if cls.name == name and cls.available():
            return cls()
    return None
