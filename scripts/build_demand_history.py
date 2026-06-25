"""Phase D-6: 約1年の日次需要履歴を生成して demand_history へ取り込む（予測用）。

会計用の実取引（6月）とは分離。SARIMA/LightGBM が効くよう、曜日・年次季節・緩い増加トレンドを
持たせた決定論的な系列を 2025-06-01〜2026-05-31 で作り、CSV取込API(/api/import/sales-history)へ。
"""
from __future__ import annotations

import json
import math
import urllib.request
from datetime import date, timedelta

INV = "http://127.0.0.1:8056"

# (sku, product_name, 1日あたり平均需要, 位相)
BASE = [
    ("USB-C-1M", "USB-Cケーブル 1m", 6.0, 0.0, 980),
    ("WL-MOUSE", "ワイヤレスマウス", 2.6, 1.1, 2480),
    ("MON-24", "24インチモニター", 0.8, 2.0, 19800),
    ("USB-HUB", "USBハブ 4ポート", 2.8, 3.2, 2980),
    ("PC-STAND", "ノートPCスタンド", 1.8, 4.0, 3480),
]

start = date(2025, 6, 1)
end = date(2026, 5, 31)
num_days = (end - start).days + 1

lines = ["date,sku,product_name,quantity,unit_price"]
total = 0
for i in range(num_days):
    d = start + timedelta(days=i)
    wf = 1.0 if d.weekday() < 5 else 0.45            # 平日多め・週末少なめ（B2B）
    trend = 1.0 + 0.0007 * i                          # 1年で緩く増加
    season = 1.0 + 0.25 * math.sin(2 * math.pi * (i / 365.0))  # 年次季節
    for sku, name, base, phase, price in BASE:
        wiggle = 1.0 + 0.18 * math.sin(i * 0.7 + phase)
        qty = max(0, round(base * wf * trend * season * wiggle))
        if qty > 0:
            lines.append(f"{d.isoformat()},{sku},{name},{qty},{price}")
            total += 1

csv_text = "\n".join(lines)
data = json.dumps({"csv": csv_text}).encode("utf-8")
req = urllib.request.Request(INV + "/api/import/sales-history", data=data, method="POST",
                            headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=60) as r:
    res = json.loads(r.read().decode("utf-8"))
print(f"generated rows: {total} ({num_days} days)")
print("import:", {k: res[k] for k in ("imported", "created_products", "skipped")})

# 取込後も突合は不変（demand_history は会計に流れない）ことを確認。
with urllib.request.urlopen(INV + "/api/reconciliation", timeout=30) as r:
    recon = json.loads(r.read().decode("utf-8"))
print("reconciliation all_match after demand import:", recon["all_match"])
