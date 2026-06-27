"""Phase D Step1（方法2）: ライブ(Render/Neon)へサンプルデータを1回だけ投入する。

★これは「あなたのPCで」「あなたしか持っていない接続情報を使って」1回だけ実行するスクリプトです。
  公開サイトには何も足しません。実行に必要な環境変数（このチャットには絶対に貼らないこと）:
    DATABASE_URL          : 在庫アプリ本番DB(Neon)の接続文字列
    PSEUDO_FREEE_DB_URL   : 疑似freee本番DB(Neon)の接続文字列  ← 古いデータ消去に使う
    PSEUDO_FREEE_API_URL  : 疑似freeeの公開URL（例 https://pseudo-freee.onrender.com）
  既定は「下見（dry-run）」。実際に消して入れ直すには末尾に --yes を付けて実行する。
  注意: 在庫・疑似freee の本番データを消して作り直します（初期化は1回だけの想定）。
"""
from __future__ import annotations

import math
import os
import sys
from datetime import date, timedelta

REQUIRED = ("DATABASE_URL", "PSEUDO_FREEE_DB_URL", "PSEUDO_FREEE_API_URL")
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    sys.exit("環境変数が未設定です: " + ", ".join(missing) + "（このチャットには貼らないこと）")

APPLY = "--yes" in sys.argv  # 付けない限り下見のみ（破壊しない）
PF_DB_URL = os.environ["PSEUDO_FREEE_DB_URL"]

# 在庫アプリは DATABASE_URL を見る。送信先は PSEUDO_FREEE_API_URL（どちらも import 前に環境にある）。
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "inventory_dashboard"))
import app  # noqa: E402

# --- 投入するデモデータ（ローカルで突合オールグリーンを確認済みの構成） --------------------
PRODUCTS = [
    ("USB-C-1M", "USB-Cケーブル 1m", 490, 980, "アキバ電子"),
    ("WL-MOUSE", "ワイヤレスマウス", 1200, 2480, "テックサプライ"),
    ("MON-24", "24インチモニター", 12000, 19800, "ディスプレイ卸"),
    ("USB-HUB", "USBハブ 4ポート", 1500, 2980, "アキバ電子"),
    ("PC-STAND", "ノートPCスタンド", 1800, 3480, "テックサプライ"),
]
PURCHASES = [
    ("USB-C-1M", "2026-06-02", 200, "アキバ電子", "P-2026-06-001"),
    ("WL-MOUSE", "2026-06-02", 80, "テックサプライ", "P-2026-06-002"),
    ("MON-24", "2026-06-03", 30, "ディスプレイ卸", "P-2026-06-003"),
    ("USB-HUB", "2026-06-03", 100, "アキバ電子", "P-2026-06-004"),
    ("PC-STAND", "2026-06-04", 60, "テックサプライ", "P-2026-06-005"),
]
CUSTOMERS = ["オフィス相模", "スタートアップ田中商店", "個人ユーザーK"]
# 6月の売上は「日次の小口」で月合計を作る＝過去の日次需要と地続きにする（チャートの「6月だけ大口
# スパイク→7月急落」を解消）。月合計は固定なので会計・突合・期末在庫は不変。
SALE_END = date(2026, 6, 25)
# (sku, 6月の月合計, 売上開始日=仕入の翌日, 波の位相)
SALES_PLAN = [
    ("USB-C-1M", 165, date(2026, 6, 3), 0.0),
    ("WL-MOUSE", 60, date(2026, 6, 3), 1.1),
    ("MON-24", 20, date(2026, 6, 4), 2.0),
    ("USB-HUB", 85, date(2026, 6, 4), 3.2),
    ("PC-STAND", 45, date(2026, 6, 5), 4.0),
]
# 棚卸減耗のデモ: 商品ごとに実地数量へ評価減（帳簿>実地→棚卸減耗損）。
SHRINKAGE = [("MON-24", 9)]  # 24インチモニター: 帳簿10 → 実地9（1個・¥12,000 の減耗）


def distribute_daily(total, start, end, phase):
    """total個を [start,end] の日次に配分（平日多め・週末少なめ＋ゆるい波）。整数で合計=total。"""
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    weights = []
    for i, d in enumerate(days):
        wf = 1.0 if d.weekday() < 5 else 0.45
        wiggle = 1.0 + 0.18 * math.sin(i * 0.7 + phase)
        weights.append(max(0.0, wf * wiggle))
    s = sum(weights) or 1.0
    raw = [total * w / s for w in weights]
    floors = [int(x) for x in raw]
    rem = total - sum(floors)
    for idx in sorted(range(len(days)), key=lambda j: raw[j] - floors[j], reverse=True)[:rem]:
        floors[idx] += 1
    return [(days[i].isoformat(), floors[i]) for i in range(len(days)) if floors[i] > 0]
DEMAND_BASE = [
    ("USB-C-1M", "USB-Cケーブル 1m", 6.0, 0.0, 980),
    ("WL-MOUSE", "ワイヤレスマウス", 2.6, 1.1, 2480),
    ("MON-24", "24インチモニター", 0.8, 2.0, 19800),
    ("USB-HUB", "USBハブ 4ポート", 2.8, 3.2, 2980),
    ("PC-STAND", "ノートPCスタンド", 1.8, 4.0, 3480),
]


def pf_clear(url: str) -> None:
    """疑似freee の本番DBから既存の取引・証憑・期末棚卸を消す（マスタ・期首残高は残す）。

    削除順が重要: deals を参照する pseudo_freee_vouchers（カスケード無し）を先に消す。
    deal_lines は ON DELETE CASCADE だが明示的にも消す。
    """
    tables = (
        "pseudo_freee_vouchers",       # deals を参照（カスケード無し＝先に消す必要）
        "pseudo_freee_deal_lines",     # deals を参照（CASCADE だが明示）
        "pseudo_freee_deals",
        "pseudo_freee_closing_inventory",
    )
    if url.startswith("postgres"):
        import psycopg

        with psycopg.connect(url) as conn:
            cur = conn.cursor()
            for t in tables:
                cur.execute(f"DELETE FROM {t}")
            conn.commit()
    else:  # ローカルテスト用（SQLite ファイルパス）
        import sqlite3

        conn = sqlite3.connect(url, timeout=15)
        for t in tables:
            try:
                conn.execute(f"DELETE FROM {t}")
            except sqlite3.OperationalError:
                pass
        conn.commit()
        conn.close()


def wait_for_pseudo_freee() -> None:
    """疑似freee が応答するまで待つ（Render 無料枠はスリープ→コールドスタートに数十秒）。

    起動前に送信すると接続拒否になり、途中失敗→片側だけ反映の不整合になりうるため、先に待つ。
    """
    import time
    import urllib.error
    import urllib.request

    url = app.PSEUDO_FREEE_API_URL.rstrip("/") + "/api/reconciliation"
    for attempt in range(1, 13):  # 最大 ~120 秒
        try:
            with urllib.request.urlopen(url, timeout=10) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError):
            pass
        print(f"  疑似freee 起動待ち... ({attempt}/12)")
        time.sleep(10)
    sys.exit("疑似freee に接続できませんでした。URL を確認し、ブラウザで一度開いてから再実行してください。")


def demand_csv() -> str:
    start, end = date(2025, 6, 1), date(2026, 5, 31)
    num_days = (end - start).days + 1
    lines = ["date,sku,product_name,quantity,unit_price"]
    for i in range(num_days):
        d = start + timedelta(days=i)
        wf = 1.0 if d.weekday() < 5 else 0.45
        trend = 1.0 + 0.0007 * i
        season = 1.0 + 0.25 * math.sin(2 * math.pi * (i / 365.0))
        for sku, name, base, phase, price in DEMAND_BASE:
            wiggle = 1.0 + 0.18 * math.sin(i * 0.7 + phase)
            qty = max(0, round(base * wf * trend * season * wiggle))
            if qty > 0:
                lines.append(f"{d.isoformat()},{sku},{name},{qty},{price}")
    return "\n".join(lines)


def main() -> None:
    with app.get_conn() as conn:
        orgs = conn.execute("SELECT id, name FROM organizations ORDER BY id").fetchall()
    if not orgs:
        sys.exit("組織が見つかりません。先にライブにログインして組織を作成してください。")
    org_id = int(os.environ.get("ORG_ID") or orgs[0]["id"])
    org_name = next((o["name"] for o in orgs if int(o["id"]) == org_id), "?")

    print("=== 投入計画（方法2・ライブ） ===")
    print(f"  在庫DB(DATABASE_URL): {'postgres(Neon)' if app.db.is_postgres() else 'ローカルSQLite'}")
    print(f"  疑似freee 送信先     : {app.PSEUDO_FREEE_API_URL}")
    print(f"  疑似freee DB消去先   : {'postgres(Neon)' if PF_DB_URL.startswith('postgres') else 'ローカルSQLite'}")
    print(f"  対象組織             : id={org_id} ({org_name})  ※全{len(orgs)}組織")
    print(f"  投入内容             : 商品{len(PRODUCTS)} / 仕入{len(PURCHASES)} / 売上(6月月合計){sum(t for _, t, _, _ in SALES_PLAN)}個を日次小口 / 需要履歴(約1年)")
    if not APPLY:
        print("\n[下見のみ] 実際に消して入れ直すには、末尾に --yes を付けて再実行してください。")
        return

    print("\n[実行] 疑似freee の起動を確認（無料枠スリープ時はコールドスタートを待つ）...")
    wait_for_pseudo_freee()
    print("[実行] 疑似freee の既存データを消去 ...")
    pf_clear(PF_DB_URL)

    with app.get_conn() as conn:
        print("[実行] 在庫の対象組織データを消去 ...")
        app.db.clear_organization_data(conn, org_id)

        for sku, name, pp, sp, sup in PRODUCTS:
            app.create_product(conn, org_id, {
                "sku": sku, "product_name": name, "category": "PC周辺機器", "supplier_name": sup,
                "purchase_unit_price": pp, "sales_unit_price": sp, "tax_rate": 10,
                "safety_stock": 5, "reorder_point": 10, "lead_time_days": 5,
            })
        prods = {p["sku"]: p for p in app.list_products(conn, org_id)}
        for sku, d, qty, sup, inv in PURCHASES:
            p = prods[sku]
            app.create_purchase(conn, org_id, {
                "product_id": p["id"], "partner_name": sup, "invoice_no": inv,
                "transaction_date": d, "received_date": d, "quantity": qty,
                "unit_price": p["purchase_unit_price"], "tax_rate": 10, "due_date": "2026-07-31",
            })
        n = 0
        for sku, total, start, phase in SALES_PLAN:
            p = prods[sku]
            for d, qty in distribute_daily(total, start, SALE_END, phase):
                n += 1
                app.create_sale(conn, org_id, {
                    "product_id": p["id"], "partner_name": CUSTOMERS[n % len(CUSTOMERS)],
                    "invoice_no": f"S-2026-06-{n:03d}", "transaction_date": d, "quantity": qty,
                    "unit_price": p["sales_unit_price"], "tax_rate": 10, "due_date": "2026-07-31",
                })
        print("[実行] 棚卸減耗（実地棚卸）を記録 ...")
        for sku, physical_qty in SHRINKAGE:
            r = app.record_shrinkage(conn, org_id, {"product_id": prods[sku]["id"], "physical_quantity": physical_qty})
            print(f"        {sku}: 実地{physical_qty} → 評価減 {r['delta']} 個")
        print("[実行] freee へ一括送信 ...")
        send = app.send_all_pending_queue(conn, org_id)
        print(f"        送信 {send['sent']} 件 / 失敗 {send['failed']} 件")
        print("[実行] 期末在庫を送信（帳簿/実地・棚卸減耗を会計へ） ...")
        closing = app.push_closing_inventory(conn, org_id, {"period": "202606"})
        print(f"        期末在庫 帳簿={closing['book_amount']:.0f} 実地={closing['physical_amount']:.0f}")
        print("[実行] 需要履歴(約1年)を取込 ...")
        summary = app.import_sales_history(conn, org_id, demand_csv())
        print(f"        需要履歴 {summary['imported']} 行")
        print("[実行] 予測バッチを実行（発注判定・必要水準を更新） ...")
        from forecasting import service as forecast_service
        fc = forecast_service.run_forecast(conn, org_id, horizon_days=30, actor_user_id=None)
        print(f"        予測: best={fc.get('best_model')} / {fc.get('products_forecasted')}商品")
        print("[確認] 突合 ...")
        recon = app.reconciliation(conn, org_id)

    print(f"\n=== 突合結果: all_match={recon['all_match']} (freee_available={recon['freee_available']}) ===")
    for row in recon["rows"]:
        d = row["diff"]
        print(f"  {row['label']}: 在庫={row['inventory']:.0f} freee={row['freee']} 差分={d} 一致={row['match']}")
    print("\n完了。ライブの「会計突合」で一致マークを確認してください。")


if __name__ == "__main__":
    main()
