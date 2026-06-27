"""Phase D-6 ローカル・サンプルデータ構築（dev サーバ経由）。

在庫(8056)・疑似freee(8010) を dev で起動した状態で実行する。既存デモ商品ラインで
「約1ヶ月の整合した実取引」＋「約1年の日次需要履歴」を生成し、突合オールグリーンを検証する。
このスクリプトはリポジトリにコミットしない（ワンショットのデータ投入用）。
"""
from __future__ import annotations

import json
import urllib.request

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

# --- 3. 月中に売上（出庫）。期末在庫が残るよう合計＜仕入。 -------------------
CUSTOMERS = ["オフィス相模", "スタートアップ田中商店", "個人ユーザーK"]
# USB-C-1M / USB-HUB は月末に追加売上を入れ、在庫を必要水準より下げて「発注を促す」表示を作る。
SALES = {
    "USB-C-1M": [("2026-06-05", 40), ("2026-06-11", 30), ("2026-06-17", 50), ("2026-06-23", 30), ("2026-06-25", 15)],
    "WL-MOUSE": [("2026-06-06", 15), ("2026-06-12", 20), ("2026-06-18", 10), ("2026-06-24", 15)],
    "MON-24": [("2026-06-07", 5), ("2026-06-13", 4), ("2026-06-19", 6), ("2026-06-24", 5)],
    "USB-HUB": [("2026-06-08", 20), ("2026-06-14", 15), ("2026-06-20", 20), ("2026-06-24", 15), ("2026-06-25", 15)],
    "PC-STAND": [("2026-06-09", 12), ("2026-06-15", 10), ("2026-06-21", 13), ("2026-06-23", 10)],
}
# 棚卸減耗のデモ: 商品ごとに実地数量を入力して在庫を評価減（帳簿>実地→棚卸減耗損）。
SHRINKAGE = [("MON-24", 9)]  # 24インチモニター: 帳簿10 → 実地9（1個・¥12,000 の減耗）
n = 0
for sku, rows in SALES.items():
    p = prods[sku]
    for i, (d, qty) in enumerate(rows):
        n += 1
        call("POST", "/api/sales", {
            "product_id": p["id"], "partner_name": CUSTOMERS[(i) % len(CUSTOMERS)],
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
