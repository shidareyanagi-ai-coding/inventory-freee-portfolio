# 実運用化（実データ運用）仕様メモ — A-9

> 軽い仕様（1枚もの）。デモから「実際の過去データで使う」ための2機能を定義する。
> 大きな会計モデル変更（複式簿記）とは別の、在庫アプリ内の中〜小フェーズ。

## 背景

デモは新規サインアップ時に**デモデータ（商品・2年分の販売履歴）を自動投入**し、それを使って
需要予測（ML）を即座に見せる。実運用では、デモではなく**自分の過去データ**で予測したい。
そのための最小機能を追加する。

## A. クリーンスタート（デモ全消去）

- **目的**: デモデータを消し、自分の実データだけで始める。
- **動作**: 自組織の業務データ（商品・取引・在庫履歴・予測・証憑・freeeキュー等）を全削除する。
  **organizations / memberships は残す**＝アカウントとログインは維持。
- **安全性**: 各組織が**自分の `organization_id` 配下にのみ** DELETE（DROP しない・他テナント不可）。admin 限定。
- 実装: `db.clear_organization_data(conn, organization_id)` ／ `POST /api/org/clear-data`（admin）。

## B. 売上履歴 CSV 一括取込

- **目的**: 過去の売上をまとめて登録し、**初日から実データで ML 予測**できるようにする
  （フォームでの1件ずつ入力では現実的でないため）。
- **CSV 形式**（1行目に列名・大文字小文字/空白は無視）:

  | 列 | 必須 | 内容 |
  |---|---|---|
  | `date` | ○ | 取引日 `YYYY-MM-DD` |
  | `sku` | ○ | 商品コード |
  | `product_name` | – | 商品名（新規 sku のとき採用。空なら sku を流用） |
  | `quantity` | ○ | 数量（正の整数） |
  | `unit_price` | – | 単価（新規商品の売価に採用。空は 0） |

- **動作**:
  - 既存商品は `sku` で照合、**未知の sku は商品を新規作成**。
  - 各行を **`sales` ＋ `inventory_movements`(movement_type='sale')** として「履歴」投入。
    予測 `forecasting.data.load_demand_series` はこの sales×movement を読むので、取込後に予測が効く。
  - **在庫検証なし・freee 送信キューに入れない**（過去データの取込であり、現在の出荷や会計連携ではないため）。
  - 商品ごとに**初期在庫(=取込売上の合計)を最古日の前日**に入れ、履歴期間中ずっと在庫が負にならないようにする。
  - 行単位のエラー（日付不正・sku 空・数量不正）は**スキップして集計**（全体は止めない）。
- **戻り値**: `{imported, created_products, skipped, errors[]}`。
- 実装: `import_sales_history(conn, organization_id, csv_text)` ／ `POST /api/import/sales-history`（admin/staff）。
- **使い方（UI）**: ダッシュボード下部「🗂 実データ運用」→ CSV を選んで取込 → 「需要予測レベル2」で**予測バッチを実行**。

## 想定する実運用フロー

1. サインアップ（自分専用組織が作られ、デモデータが入る）
2. **A. デモデータを全消去**（クリーンスタート）
3. **B. 過去の売上 CSV を取込**（商品も自動作成される）
4. 「予測バッチを実行」→ **自分の実データで需要予測**

## スコープ外（今後の候補）

- 商品マスタ専用 CSV（リードタイム・安全在庫など詳細列の一括設定）
- 仕入履歴 CSV（現在庫を正確に再現する場合）
- サインアップ時にデモを入れない設定（今は「入れてから消す」方式）

## テスト

- `inventory_dashboard/test_app.py`:
  `test_import_sales_history_feeds_forecast`（取込→予測が需要を読む）/
  `test_import_sales_history_skips_invalid_rows`（不正行スキップ）/
  `test_clear_organization_data_empties_but_keeps_account`（業務データ全消去・アカウント残存）。
