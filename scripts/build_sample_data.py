"""Phase D-6 ローカル・サンプルデータ構築（dev サーバ経由）。

在庫(8056)・疑似freee(8010) を dev で起動した状態で実行する。既存デモ商品ラインで
「約1ヶ月の整合した実取引」＋「約1年の日次需要履歴」を生成し、突合オールグリーンを検証する。
このスクリプトはリポジトリにコミットしない（ワンショットのデータ投入用）。
"""
from __future__ import annotations

import json
import math
import urllib.request
from datetime import date, timedelta

INV = "http://127.0.0.1:8056"


def call(method: str, path: str, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        INV + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


# --- 0. 在庫をクリーンスタート（dev=admin） ---------------------------------
print("clear:", call("POST", "/api/org/clear-data"))

# --- 1. 既存デモ商品ライン（PC周辺機器/事務用品店） -------------------------
PRODUCTS = [
    ("USB-C-1M", "USB-Cケーブル 1m", 490, 980, "アキバ電子"),
    ("WL-MOUSE", "ワイヤレスマウス", 1200, 2480, "テックサプライ"),
    ("MON-24", "24インチモニター", 12000, 19800, "ディスプレイ卸"),
    ("USB-HUB", "USBハブ 4ポート", 1500, 2980, "アキバ電子"),
    ("PC-STAND", "ノートPCスタンド", 1800, 3480, "テックサプライ"),
]
for sku, name, pp, sp, sup in PRODUCTS:
    call("POST", "/api/products", {
        "sku": sku, "product_name": name, "category": "PC周辺機器",
        "supplier_name": sup, "purchase_unit_price": pp, "sales_unit_price": sp,
        "tax_rate": 10, "safety_stock": 5, "reorder_point": 10, "lead_time_days": 5,
    })
prods = {p["sku"]: p for p in call("GET", "/api/products")}
print("products:", len(prods))

# --- 2. 月初に仕入（入庫＝在庫を作る） --------------------------------------
PURCHASES = [
    ("USB-C-1M", "2026-06-02", 200, "アキバ電子", "P-2026-06-001"),
    ("WL-MOUSE", "2026-06-02", 80, "テックサプライ", "P-2026-06-002"),
    ("MON-24", "2026-06-03", 30, "ディスプレイ卸", "P-2026-06-003"),
    ("USB-HUB", "2026-06-03", 100, "アキバ電子", "P-2026-06-004"),
    ("PC-STAND", "2026-06-04", 60, "テックサプライ", "P-2026-06-005"),
]
for sku, d, qty, sup, inv in PURCHASES:
    p = prods[sku]
    call("POST", "/api/purchases", {
        "product_id": p["id"], "partner_name": sup, "invoice_no": inv,
        "transaction_date": d, "received_date": d, "quantity": qty,
        "unit_price": p["purchase_unit_price"], "tax_rate": 10, "due_date": "2026-07-31",
    })
print("purchases:", len(PURCHASES))

# --- 3. 月中に売上（出庫）。日次の小口で月合計を作る＝過去の日次需要と地続きにする。 ----
# 各商品の6月“月合計”は固定（USB-C 165 など）。会計・突合・期末在庫は月合計が同じなので不変。
# 一方チャートの実績は年間通して連続した日次になり、「6月だけ大口スパイク→7月急落」の不自然さを解消する。
# 月合計は仕入＜売上にせず期末在庫を残す（USB-C: 仕入200・売上165→在庫35 など）。
CUSTOMERS = ["オフィス相模", "スタートアップ田中商店", "個人ユーザーK"]
SALE_END = date(2026, 6, 25)
# (sku, 6月の月合計, 売上開始日=仕入の翌日, 波の位相)
SALES_PLAN = [
    ("USB-C-1M", 165, date(2026, 6, 3), 0.0),
    ("WL-MOUSE", 60, date(2026, 6, 3), 1.1),
    ("MON-24", 20, date(2026, 6, 4), 2.0),
    ("USB-HUB", 85, date(2026, 6, 4), 3.2),
    ("PC-STAND", 45, date(2026, 6, 5), 4.0),
]
# 棚卸減耗のデモ: 商品ごとに実地数量を入力して在庫を評価減（帳簿>実地→棚卸減耗損）。
SHRINKAGE = [("MON-24", 9)]  # 24インチモニター: 帳簿10 → 実地9（1個・¥12,000 の減耗）


def distribute_daily(total, start, end, phase):
    """total個を [start,end] の日次に配分（平日多め・週末少なめ＋ゆるい波）。整数で合計=total。"""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    weights = []
    for i, d in enumerate(days):
        wf = 1.0 if d.weekday() < 5 else 0.45            # 平日多め・週末少なめ（B2B）
        wiggle = 1.0 + 0.18 * math.sin(i * 0.7 + phase)
        weights.append(max(0.0, wf * wiggle))
    s = sum(weights) or 1.0
    raw = [total * w / s for w in weights]
    floors = [int(x) for x in raw]
    rem = total - sum(floors)                            # 端数を最大剰余法で配り、合計を total に一致させる
    for idx in sorted(range(len(days)), key=lambda j: raw[j] - floors[j], reverse=True)[:rem]:
        floors[idx] += 1
    return [(days[i].isoformat(), floors[i]) for i in range(len(days)) if floors[i] > 0]


n = 0
for sku, total, start, phase in SALES_PLAN:
    p = prods[sku]
    for d, qty in distribute_daily(total, start, SALE_END, phase):
        n += 1
        call("POST", "/api/sales", {
            "product_id": p["id"], "partner_name": CUSTOMERS[n % len(CUSTOMERS)],
            "invoice_no": f"S-2026-06-{n:03d}", "transaction_date": d, "quantity": qty,
            "unit_price": p["sales_unit_price"], "tax_rate": 10, "due_date": "2026-07-31",
        })
print("sales:", n)

# --- 3.5 棚卸減耗（実地棚卸）。商品ごとに実地数量へ評価減する。 ---------------
for sku, physical_qty in SHRINKAGE:
    r = call("POST", "/api/shrinkage", {"product_id": prods[sku]["id"], "physical_quantity": physical_qty})
    print(f"shrinkage {sku}: delta={r['delta']}")

# --- 4. freee へ一括送信 ----------------------------------------------------
print("send-all:", call("POST", "/api/freee-sync-queue/send-all"))

# --- 5. 期末在庫を freee へ（実地=帳簿。current 時点） ----------------------
print("closing push:", call("POST", "/api/closing-inventory/push", {"period": "202606"}))

# --- 6. 突合（オールグリーンを期待） ---------------------------------------
recon = call("GET", "/api/reconciliation")
print("reconciliation all_match:", recon["all_match"])
for row in recon["rows"]:
    print(f"  {row['label']}: 在庫={row['inventory']:.0f} freee={row['freee']:.0f} diff={row['diff']:.0f} match={row['match']}")
