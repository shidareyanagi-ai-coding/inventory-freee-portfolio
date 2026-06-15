"""現実的な日次需要の合成データ生成（EVOLUTION_PLAN.md A-4）。

レベル2 の土台。月次2点の粗いデモ履歴を、2年・日次の「それらしい」需要に作り直す。
需要 = base × トレンド × 週次季節 × 月次季節 × イベント（補助金/キャンペーン）× ノイズ。

設計方針:
- **stdlib のみ**（numpy/pandas に依存しない）。seed 処理は重いML依存が無くても動くべきだから。
- **決定論的**: `random.Random(seed)` 固定で再現可能（Date.now/グローバル乱数を使わない）。
- イベントは需要にスパイクを与えると同時に `external_factors` にも記録され、
  LightGBM が「カレンダーだけでは読めない要因」を学習できる見せ場になる。
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

# 平日高・週末低（B2B 想定）。月=0 ... 日=6。
WEEKDAY_MULTIPLIER = [1.15, 1.12, 1.05, 1.10, 1.22, 0.55, 0.40]

# 季節の山になる月（強さは商品ごとの season_strength で調整）。
SEASONAL_PEAK_MONTHS = {3, 6, 11, 12}

# イベント種別ごとの需要倍率（factor_type → multiplier）。
EVENT_MULTIPLIER = {"補助金": 1.6, "キャンペーン": 1.95}

# トレンド: 期間の最初から最後にかけて +20% 程度ゆるやかに伸びる。
TREND_TOTAL_GROWTH = 0.20

DEFAULT_EVENT_SEED = 20240101


def generate_events(start: date, end: date, seed: int = DEFAULT_EVENT_SEED) -> dict[str, str]:
    """期間内の組織横断イベントを生成し {日付ISO: factor_type} で返す。

    - 補助金: 年あたり数回・各 5〜10 日間の窓。
    - キャンペーン: 単発日。
    需要スパイクの「種」になり、`external_factors` にも記録される。
    """
    rng = random.Random(seed)
    span_days = max((end - start).days, 1)
    events: dict[str, str] = {}

    n_subsidy_windows = max(2, span_days // 240)
    for _ in range(n_subsidy_windows):
        offset = rng.randint(0, max(span_days - 10, 1))
        window_start = start + timedelta(days=offset)
        length = rng.randint(5, 10)
        for k in range(length):
            day = window_start + timedelta(days=k)
            if start <= day <= end:
                events[day.isoformat()] = "補助金"

    n_campaign_days = max(4, span_days // 90)
    for _ in range(n_campaign_days):
        offset = rng.randint(0, span_days)
        day = start + timedelta(days=offset)
        if start <= day <= end:
            # 既に補助金窓の日ならそちらを優先（上書きしない）。
            events.setdefault(day.isoformat(), "キャンペーン")

    return events


def _month_season(month: int, season_strength: float) -> float:
    return season_strength if month in SEASONAL_PEAK_MONTHS else 1.0


def daily_demand_series(
    base: float,
    season_strength: float,
    start: date,
    end: date,
    events: dict[str, str],
    seed: int,
) -> list[tuple[date, int]]:
    """[start, end] の各日について (日付, 需要数量) を返す（需要0の日もあり得る）。"""
    rng = random.Random(seed)
    total_days = max((end - start).days, 1)
    series: list[tuple[date, int]] = []

    day = start
    index = 0
    while day <= end:
        trend = 1.0 + (index / total_days) * TREND_TOTAL_GROWTH
        weekly = WEEKDAY_MULTIPLIER[day.weekday()]
        monthly = _month_season(day.month, season_strength)
        event_type = events.get(day.isoformat())
        event = EVENT_MULTIPLIER.get(event_type, 1.0) if event_type else 1.0
        noise = rng.uniform(0.78, 1.22)
        quantity = base * trend * weekly * monthly * event * noise
        series.append((day, max(int(round(quantity)), 0)))
        day += timedelta(days=1)
        index += 1

    return series


def seed_for_sku(sku: str) -> int:
    """SKU から決定論的な乱数シードを作る（process 間で安定。hash() は使わない）。"""
    value = 0
    for position, char in enumerate(sku):
        value = (value * 131 + ord(char) * (position + 1)) % 2_147_483_647
    return value + 1
