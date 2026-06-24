# 計画書: 疑似freee に簡易・複式簿記（試算表／BS／PL）＋在庫→freee 期末棚卸連携

> **状態: Phase A 実装完了（2026-06-23）。Phase B（在庫→freee 期末棚卸API連携）は未着手。**
> 実装ブランチ: `feature/double-entry-bookkeeping`。
> このドキュメントが実装の正本。指示は「`docs/DOUBLE_ENTRY_BOOKKEEPING_PLAN.md` に沿って実装して」でOK。
>
> **Phase A 実装メモ（2026-06-23）**
> - `pseudo_freee/db.py`: `pseudo_freee_account_items` に `account_category`/`statement`/`normal_balance` を追加（両DDL）。新テーブル `pseudo_freee_opening_balances`・`pseudo_freee_closing_inventory` を両DDL＋`PSEUDO_FREEE_TABLES` に追加。
> - `pseudo_freee/app.py`: `ACCOUNT_CLASSIFICATION` ほか定数、科目分類/期首/期末の seed（`seed_account_classification`・`seed_opening_balances`・`seed_closing_inventory`）、計算器（`derive_journal_entries`・`account_balances`・`closing_adjustments`＝三分法・`calculate_trial_balance`/`_income_statement`/`_balance_sheet`）、決算ページ `render_statements` ＋ ルート `/statements`・`/api/statements`・ナビ「決算」。MASTER_SQL/ensure_master_schema も追従。
> - **設計上の追加判断**: 構造科目（現金・売掛金…・売上高・売上原価）はマスタには持つが、手入力経費の勘定候補からは除外（`NON_EXPENSE_ACCOUNT_ITEMS`／`list_expense_masters` でフィルタ）。期末商品は `pseudo_freee_closing_inventory` の最新 period の `physical_amount`（Phase A は seed のデモ値）。
> - **テスト**: `pseudo_freee/test_app.py` に `PseudoFreeeBookkeepingTest`（仕訳貸借一致・試算表・BS・純利益→純資産・三分法・期首一致・期末上書き・決算ページ描画・経費候補除外）。全体 `pytest` 55 passed / 5 skipped(Postgres)。
> - **ローカル実機検証済**: 既存 SQLite DB（9取引）に対し列追加＋seed が in-place 移行され、`/statements` で 試算表/BS とも「貸借一致 ✓」、`/`(KPI) と `/api/deals` は回帰なし。
> - **残**: 🧑 ライブ(Render)目視、main マージ、Phase B 着手。
>
> **Phase A 追補（2026-06-23・ユーザ要望で見える化を強化）**
> - ダッシュボード(`/`)が旧画面と同じに見える指摘を受け、**決算導線を前面化**: `/` 冒頭に「決算書を見る／印刷」バナー（当期純利益＋貸借一致バッジ）。決算は引き続き `/statements`。
> - `/statements` を全面拡充: **① 決算手続き入力フォーム**（期末商品棚卸高＝実地・減価償却費／POST `/closing`→`save_closing_procedure`。在庫DB反映ボタンは Phase B 用に無効表示）、**② 決算整理仕訳**（`closing_journal` が唯一の正＝表示と `account_balances` の③が同じ関数）、③PL ④BS ⑤試算表、**⑥仕訳帳**（`journal_transactions`＝開始記入＋期中＋決算整理）、**⑦総勘定元帳**（`general_ledger`＝科目別の走り残高）、**印刷/PDF対応**（`@media print`＋`window.print()`、`.no-print`）。
> - **減価償却（ユーザ選択＝定額法・間接法）**: 新勘定 `減価償却費`(費用) と `減価償却累計額`(資産の評価勘定＝貸方残高・BSで備品のマイナス表示)。期首`備品`÷耐用年数(`DEPRECIATION_USEFUL_LIFE_YEARS`=5)=年¥40,000を seed、`/closing` で上書き可。新テーブル `pseudo_freee_closing_settings`(period, depreciation_amount／両DDL＋`PSEUDO_FREEE_TABLES`)＋`seed_closing_settings`。
> - テスト追加で **pytest 62 passed / 5 skipped**。実機(既存9取引DB)で `/` バナー＋`/statements` ①〜⑦・印刷ボタン・貸借一致 ✓ を確認。**サーバ再起動が必要**（既存プロセスは旧コード）。
>
> **Phase A 追補2（2026-06-23・ユーザ要望でビュー分割）**
> - `/statements` を**3ビューのタブUI**に再編（決算書／仕訳帳／総勘定元帳）。タブボタン＋`?view=statements|journal|ledger` で深いリンク（ダッシュボードの3ボタンが各ビューへ）。JSは `_STATEMENTS_VIEW_JS`（hidden トグル）、初期ビューは `window.__STMT_VIEW__`、`render_statements(active_view)`／GET `/statements?view=`。
> - **総勘定元帳**は勘定科目セレクタ(`#ledger-account`)で選んだ1科目だけ表示。各記入に**相手勘定**列を追加（`_counter_account`＝反対側が1科目ならその名、複数なら『諸口』）。`general_ledger` の postings に `counter` を格納。
> - 印刷は表示中ビューのみ（`@media print` で `[hidden]` 非表示・`.no-print` 非表示）。ダッシュボード(`/`)バナーは「決算書/仕訳帳/総勘定元帳を表示」の3ボタンに変更。**pytest 64 passed / 5 skipped**・実機確認済。
>
> **Phase A 追補3（2026-06-23・印刷バグ修正＋元帳の月フィルタ）**
> - **仕訳帳の印刷で1ページ目（タイトル）しか出ない不具合を修正**: 原因は base CSS の `table { overflow: hidden }`（overflow 指定の箱は印刷でページ分割できず1ページ目で切れる）。`@media print` で `table/.card { overflow: visible }`＋`thead { display: table-header-group }`（見出し行をページ毎に繰返し）＋`tr { break-inside: avoid }` を追加し、`.card` の `break-inside: avoid` は撤去。
> - **総勘定元帳に月フィルタ**: `general_ledger(conn, month)` が `'YYYY-MM'`/`'決算'` で記入を絞り、月初に **前月繰越**(carry_forward) を付ける（動きの無い科目はその月は出さない）。`ledger_periods` が候補月を返す。元帳ビューに月セレクタ（`#ledger-month`・onchange で `/statements?view=ledger&month=` に遷移＝サーバ側で繰越計算）＋勘定科目セレクタ（client）。`render_statements(active_view, month)`／GET `?month=`。`_transaction_bucket`（期首→繰越／取引→発生月／決算整理→決算）。**pytest 66 passed / 5 skipped**・実機確認済。
>
> **Phase A 追補4（2026-06-23・仕訳帳/元帳から編集導線＝方式A）**
> - ユーザ選択=**方式A（伝票を編集→帳簿が自動更新／on-the-fly 維持）**・対象=**手入力経費＋決算整理**（期首残高は今回見送り）。
> - 仕訳帳・総勘定元帳の各行に「操作」列（`.no-print`）を追加し編集導線を出す。`_edit_target(kind, source_type, deal_id)`／`_edit_cell`: 手入力経費→`/deals/{id}/edit`（既存フォーム＝日付・科目・金額・相手勘定=支払方法）、在庫連携(purchase/sale)→「在庫側で管理」（編集不可＝在庫が正）、決算整理→`/statements?view=statements#closing-form`（決算手続きフォーム）、期首→不可。`journal_transactions` の deal に `source_type` を、`general_ledger` の posting に `kind/source_type/deal_id` を付与。決算手続き `<section id="closing-form">`。
> - **pytest 68 passed / 5 skipped**。実機で編集導線の表示＋編集ラウンドトリップ（手入力経費の金額変更→PL当期純利益が再計算）を確認（検証で触れたデモDBの取引#10は元値¥840へ復旧済）。

## Context（なぜやるか）

`inventory-freee-portfolio` の Plan A（在庫管理＋需要予測＋AI証憑＋デプロイ）は完成済み。本計画は**既存リポジトリへの追加フェーズ**で、新規プロジェクトではない（同じ2アプリ・同じDB・同じデプロイ）。

疑似freee は現在「取引(deal)を片側だけ記録する freee 風の簡易入力」で、勘定科目は名前だけ・分類なし・**複式仕訳/試算表/BS/PL は無い**。これを **簿記3級レベルの貸借対照表(BS)・損益計算書(PL)** が出るところまで発展させる。在庫アプリ側の**期末在庫評価額**を取り込んで**売上原価（三分法）**を計算し、`帳簿棚卸高`を提案・`実地棚卸高`で人が上書きできる形にする（A-5の「下書きはシステム/確定は人」と同じ思想）。

### 確定した設計判断（ユーザ承認済）
- **税込経理**（仮払/仮受消費税は作らない。金額そのまま計上）。
- **段階リリース**: Phase A（疑似freee単体でBS/PL・期末在庫はseed/手入力）→検証→ Phase B（在庫→freee の期末棚卸API連携）。
- **単一テナント**: `pseudo_freee_deals` に organization_id は無い＝疑似freeeは「1つのモック会社の帳簿」。BS/PLは全dealを集計（マルチテナント化はしない）。
- **仕訳はオンザフライ導出**（専用の仕訳テーブルは作らない。`pseudo_freee_deals` を唯一の正とし、レポート時に集計）。
- **三分法**で売上原価（期首商品＋当期仕入−期末商品）。
- 会計期間は当面**単一期間**（期首残高→全取引→期末）として扱う。年度フィルタは将来の任意拡張。

## 着手ステップ0（リポジトリ内に恒久ドキュメントを作成）
本ファイル（`docs/DOUBLE_ENTRY_BOOKKEEPING_PLAN.md`）がそれ。以降ユーザは本ファイルを指して指示できる。

---

## 重要な前提（コード調査で確定済）
- 疑似freee は **stdlib http.server**（FastAPIではない）。ルーティングは `PseudoFreeeHandler.do_GET`(app.py:2716)/`do_POST`(app.py:2778) の手書き `elif parsed.path == ...`。ページは `render_page(title, body)`(app.py:1303) でClerkゲート＋CSSを自動付与、ナビは app.py:1791。集計の型は `get_summary`(app.py:1182)/`get_monthly_trends`(app.py:1225)。
- **掛/現金の判定**: `pseudo_freee_deals.payment_method` は `create_manual_expense`(app.py:734) でしか入らない。在庫由来の `create_deal`(app.py:657) では空。→ purchase/sale は **`due_date` の有無**で判定（非空=掛、空=現金。既存の receivable/payable ロジック app.py:1200-1201 と同基準）。manual_expense は payment_method を使う。
- スキーマDDLは `pseudo_freee/db.py` の `SQLITE_SCHEMA_SQL`(44) と `POSTGRES_SCHEMA_SQL`(124) の**両方**＋ app.py:101 の `MASTER_SQL` 複製を更新。既存SQLite DBは `ensure_master_schema`(app.py:301) に `PRAGMA table_info`＋`ALTER TABLE ADD COLUMN` を追加（Postgresは早期return）。
- 在庫→疑似freee 連携: `send_queue_to_pseudo_freee`(inventory/app.py:1567) が `POST {PSEUDO_FREEE_API_URL}/api/deals`。`enqueue_freee_payload`(app.py:739) が `freee_sync_queue`(db.py:169/379) に積む。**`source_id` は INTEGER NOT NULL ＋ UNIQUE(source_type, source_id)** → 期間キーは整数(例 202603)。
- 在庫評価額: `stock_by_product`(inventory/app.py:516) は日付フィルタ無し（`movement_date` はあるのでas-of変種は容易）。評価額 = 数量 × `purchase_unit_price`、合計は `dashboard()` の total_stock_value(app.py:837)。
- `/api/deals` は purchase/sale/manual に限定（`normalize_deal_request`）→ 期末棚卸は**専用の開放エンドポイント** `/api/closing-inventory` を新設（`/api/deals` 同様に機械向け＝認証なし）。
- テスト: 疑似freee `test_app.py`＋`conftest.py` は `app.DB_PATH` を一時ファイルにし `init_db()`→業務関数を直接呼ぶ(SQLite強制)。在庫 `test_api.py` は `AUTH_DEV_MODE=true` の TestClient。

---

## Phase A — 疑似freee 単体で BS/PL（期末在庫は seed/手入力）

### A1. 勘定科目マスタに分類を追加
`pseudo_freee_account_items` に列追加（db.py 両DDL＋app.py:101 MASTER_SQL＋ensure_master_schema のSQLite移行）:
`account_category`(資産/負債/純資産/収益/費用), `statement`(BS/PL), `normal_balance`(借/貸)。

新定数 `ACCOUNT_CLASSIFICATION`（`DEFAULT_ACCOUNT_ITEMS` 付近 app.py:163）でマッピング:
| 科目 | category | statement | normal |
|---|---|---|---|
| 現金,普通預金,売掛金,未収金,商品,建物,備品 | 資産 | BS | 借 |
| 買掛金,未払金 | 負債 | BS | 貸 |
| 資本金,繰越利益剰余金 | 純資産 | BS | 貸 |
| 売上高 | 収益 | PL | 貸 |
| 売上原価,仕入高 | 費用 | PL | 借 |
| 既存経費13科目(消耗品費…雑費) | 費用 | PL | 借 |

`seed_master_data`(app.py:368): 新BS科目を `INSERT ... ON CONFLICT DO NOTHING`、続けて `UPDATE ... SET account_category/statement/normal_balance WHERE account_item_name=? AND account_category=''`（既存 search_key 更新と同形）。

### A2. 仕訳導出（純粋関数）
`derive_journal_entries(deal) -> list[{account, side, amount}]`。相手科目は `_settlement_account(deal)`: payment_method が現金/普通預金/未払金ならそれを、無ければ due_date 有無で 掛/現金。金額は税込 `deal["amount"]`。
| source_type/deal_type | 借方 | 貸方 |
|---|---|---|
| sale/income・掛(due_date非空) | 売掛金 | 売上高 |
| sale/income・現金 | 現金 | 売上高 |
| purchase/expense・掛 | 仕入高 | 買掛金 |
| purchase/expense・現金 | 仕入高 | 現金 |
| manual_expense・未払金 | 〔費用科目〕 | 未払金 |
| manual_expense・現金/普通預金 | 〔費用科目〕 | 現金/普通預金 |

### A3. 期首残高
新テーブル `pseudo_freee_opening_balances`(account_item_name UNIQUE, amount, side)（両DDL＋`PSEUDO_FREEE_TABLES` db.py:298 追加。CREATE TABLE IF NOT EXISTS なので ensure 移行は不要）。`DEFAULT_OPENING_BALANCES` を**貸借一致する**デモ値で seed（資本金は固定値、`商品` の期首値＝期首商品棚卸高）。例: 現金30万/普通預金120万/売掛金50万/商品40万/備品20万 ＝ 借260万、買掛金30万/資本金200万/繰越利益剰余金30万 ＝ 貸260万。

### A4. 決算整理（三分法・売上原価）
`closing_adjustments(conn)`: 期首商品=opening`商品`、当期仕入=期中`仕入高`借方合計、期末商品=`pseudo_freee_closing_inventory.physical_amount`（Phase Aは `DEFAULT_CLOSING_INVENTORY` のseed/手入力）。売上原価=期首+当期仕入−期末。棚卸減耗損(任意)=帳簿−実地（Phase Aは0）。仕訳は物理生成せず、計算側で `仕入高`→0・`売上原価`を計上・`商品`を期末額に置換。

### A5. 計算器（app.py）
共通 `account_balances(conn) -> {account: 符号付残高}`: ①期首残高をnormal方向で読む ②期中dealを `derive_journal_entries` で畳み込む ③決算整理を反映。
- `calculate_trial_balance(conn)`（決算整理後）: 各科目を normal_balance で借/貸へ。`balanced = abs(借合計−貸合計) < 1`。
- `calculate_income_statement(conn)`: 売上高/売上原価/その他費用、`net_income = 売上 − 売上原価 − 費用`。
- `calculate_balance_sheet(conn)`: 資産/負債/純資産（資本金＋繰越利益剰余金＋**当期純利益**を独立行）、貸借一致チェック。
保証: 期首一致＋各deal一致＋決算整理一致 ⇒ 試算表一致 ⇒ BS一致（純利益を純資産へ）。float比較は `abs(a-b)<1`。

### A6. 決算ページ
`render_statements()` を `render_page("決算（試算表・BS・PL）", body)` で（自動Clerkゲート）。試算表＋BS（資産｜負債・純資産の2カラム）＋PL を `yen()`(app.py:1801) で整形し各々「貸借一致 ✓/✗」を表示。`do_GET`(2716) に `elif parsed.path == "/statements"`、ナビ(1791) に `<a href="/statements">決算</a>`。任意でJSON `/api/statements`。

### A7. テスト（pseudo_freee/test_app.py）
`test_journal_entries_balanced` / `test_trial_balance_debit_equals_credit` / `test_balance_sheet_balances` / `test_net_income_flows_to_equity` / `test_cogs_three_split` / `test_opening_balance_balances` / `test_statements_page_renders`。

**→ ここでローカル検証（後述）→ ユーザ確認 → Phase Bへ。**

---

## Phase B — 在庫 → 疑似freee 期末棚卸 API連携

### B1. 受け側（疑似freee）
新テーブル `pseudo_freee_closing_inventory`(period TEXT 'YYYYMM', book_amount, physical_amount, created_at, UNIQUE(period))（両DDL＋`PSEUDO_FREEE_TABLES`）。`/api/deals` は流用せず、`do_POST`(2778) に **`/api/closing-inventory`**（機械向け・認証なし）→ `upsert_closing_inventory(conn, data)`（`INSERT ... ON CONFLICT(period) DO UPDATE`）。A4 の期末商品はこの physical_amount を参照。

### B2. 送り側（在庫 inventory_dashboard/app.py）
- `stock_by_product` に任意 `as_of`（`WHERE movement_date <= ?`、None=現在）。
- `closing_inventory_book_amount(conn, org_id, as_of)` = Σ(数量 × purchase_unit_price)。
- `push_closing_inventory(conn, org_id, data)`: `period`('YYYYMM')・任意 `as_of`・任意 `physical_amount` 上書き。book算出→ `physical = override or book`。**既存 `freee_sync_queue` 経由**で監査/再送整合（`source_type='closing_inventory'`, `source_id=int(period)`）。payload `{"api_target":"pseudo_freee_closing_inventory","period":"202603","book_amount":B,"physical_amount":P}`。`send_queue_to_pseudo_freee` は `/api/deals` 固定なので closing 用に送信先を分岐 or 専用送信関数を追加。
- 新ルート `@app.post("/api/closing-inventory/push", status_code=201)`（`WRITER`＝admin/staff）＋ `record_audit(... "closing_inventory.push" ...)`。

### B3. 在庫UI（index_html.py）
「🗂 実データ運用」パネル(line~335)付近に **「決算: 期末在庫をfreeeへ送る」** パネルを `.panel/.section-head/.note` で追加。入力: 期(YYYYMM)・as-of日(任意)・「帳簿評価額を計算」・実地棚卸高(上書き)・「freeeへ送信」。JSは `salesCsvImportBtn` ハンドラ(line~1029)付近で `api('/api/closing-inventory/push', {method:'POST', body: JSON.stringify({period, as_of, physical_amount})})`。

### B4. テスト
在庫: `closing_inventory_book_amount`/payload構築の単体テスト（HTTPはCIで叩かない＝既存の URLError 経路同様にモック/分離）。疑似freee: `upsert_closing_inventory`（insert＋upsert）と、`calculate_balance_sheet` の `商品` が physical_amount を反映すること。

---

## 変更ファイル
- 疑似freee: `pseudo_freee/db.py`（両DDL: 科目3列＋新2テーブル, `PSEUDO_FREEE_TABLES`）、`pseudo_freee/app.py`（MASTER_SQL, ensure_master_schema, 定数, seed_master_data/seed_opening_balances, derive_journal_entries, account_balances, closing_adjustments, calculate_*, upsert_closing_inventory, render_statements, ルート＋ナビ）、`pseudo_freee/test_app.py`。
- 在庫: `inventory_dashboard/app.py`（as-of在庫, closing_inventory_book_amount, push_closing_inventory, ルート）、`inventory_dashboard/index_html.py`（パネル＋JS）、`inventory_dashboard/test_app.py`/`test_api.py`。
- ドキュメント: 本ファイル、README §機能 に1行追加。

## 検証（dev・エンドツーエンド）
1. 疑似freee 起動: `cd pseudo_freee && python app.py`（127.0.0.1:8010・Clerk未設定でdev通過）。
2. 在庫 起動: `cd inventory_dashboard`＋`AUTH_DEV_MODE=true` で uvicorn（`PSEUDO_FREEE_API_URL` 既定で上記）。
3. 在庫でデモseed→仕入/売上作成→既存キューで `/api/deals` へ送信。
4. **(Phase B)** 在庫の新パネルで期末在庫を計算→(任意で実地上書き)→送信。
5. 疑似freee `/statements`: 試算表 貸借一致 ✓・BS 資産=負債+純資産・PL 当期純利益＝BS純資産増分・`商品`＝期末額。
6. 回帰: 疑似freee `/`(KPI) と `/api/deals`(purchase/sale) が壊れない。
7. テスト: 疑似freee `python -m pytest test_app.py`、在庫 `python -m pytest test_api.py test_app.py`（`.venv` 使用）。

## 実装時に確認する小決定（ブロッカーではない）
1. `purchases.due_date`/`sales.due_date` が常に入るか（常に入るなら在庫取引は実質すべて掛取引＝売掛/買掛）。空がありうるなら現金分岐が効く。
2. 期首残高の実数値（資本金ほか）＝デモ用の見栄え。seed後に微調整可。
3. 棚卸減耗損（帳簿−実地）をPL計上するか注記だけか（Phase Aは0、Phase Bで任意計上）。
4. 会計期間: 当面は単一期間。年度フィルタ(env)は将来拡張。

## 工数の目安
中程度。Phase A ≈ 3〜5日相当、Phase B（API連携）＋1〜1.5日。理解・説明込みの実働で概ね1〜2週間。

---

## Phase C — 在庫⇄疑似freee の取消・修正の同期（reverse-and-repost）

> **状態: 実装完了（2026-06-23）。実装ブランチ `feature/phase-c-cancel-sync`。Phase A/B とは独立した別変更。**
>
> **Phase C 実装メモ（2026-06-23）**
> - **在庫 `inventory_dashboard/app.py`**:
>   - `_negate_freee_payload(payload)`: 元 payload の details の `quantity`/`amount` をマイナスにし `memo` に「取消」を付与（`build_freee_payload` が毎回新規 dict を返すため破壊的編集で安全）。
>   - `build_freee_payload`: 先頭で `*_cancel` を分岐＝base 型(`purchase`/`sale`)の payload を作って `_negate_freee_payload`。これで **送信前レビュー(`/api/freee-preview`)も取消仕訳をそのまま表示**できる。
>   - `enqueue_freee_cancel(conn, org, source_type, source_id)`: `source_type='purchase_cancel'/'sale_cancel'`・`source_id=元のid`・`status='pending'` で `freee_sync_queue` に INSERT（`ON CONFLICT DO UPDATE` 冪等）。在庫キューの `UNIQUE(source_type, source_id)` を `*_cancel` 型で衝突回避。
>   - `cancel_inventory_movement`: 元伝票の queue が **`sent` のとき `enqueue_freee_cancel` を呼ぶ**（戻り値に `cancel_queued: True`）。未送信(pending/failed/retry)は従来どおり `cancelled` にするだけ。元の `sent` 行はそのまま残す＝**元仕訳＋取消仕訳の両方が監査証跡**。
>   - `send_queue_to_pseudo_freee`: `source_type` が `*_cancel` のとき **base 型へ戻して POST**（疑似freee の `source_type CHECK` と整合）。`queue_id` が別なので疑似freee 側は新規 deal として保存＝マイナス金額で元仕訳を相殺。
> - **在庫 UI `index_html.py`**: 送信待ちキューで `*_cancel` を「🟥取消仕訳 仕入取消/売上取消 #id」と明示（`queueSourceLabel`）。取消実行時、送信済みなら「取消仕訳を送信待ちキューに積みました（送信で反映）」と案内（`cancelMovement` が `result.cancel_queued` を見る）。
> - **疑似freee `app.py`**: 実質無改修。`render_index` の deal 行で **`amount < 0` に「取消」バッジ**だけ追加（見える化）。集計はすべて金額の足し算なのでマイナス deal が自動相殺（仕訳・KPI・BS・PL・残高）。
> - **テスト**: 在庫 `test_app.py` に 4 件（取消payloadの符号反転・送信済み取消→取消仕訳キュー投入・未送信取消は投入しない・`*_cancel`→base マップ送信）。疑似freee `test_app.py` に 1 件（マイナス deal が PL売上/売掛金を相殺・元+取消の2行が残る）。**在庫 38 passed / 疑似freee 59 passed**（各ディレクトリで実行＝ルート一括は dual `app.py` の import 衝突で従来から不可）。
> - **クロスアプリ検証済**: 在庫が生成する取消リクエスト body を疑似freee の `create_deal` に直接投入し、`source_type='purchase'` で受理＝新規行・2 deal 保持・`買掛金` が期首水準へ復帰（元仕入を相殺）を確認。
> - **繰越/残**: 🧑 ライブ(Render)で SSO 跨ぎの一気通貫目視（仕入登録→送信→取消→取消仕訳送信→疑似freee の試算表が一致）、main マージ。「修正＝取消＋新規」運用（新規入力は既存 push で足りる）。

### 設計（参考・承認時の記録）
> **設計承認: 2026-06-23・ユーザ選択=C案。**

### 背景（コード確認済の前提）
- 在庫元帳は**追記型(append-only)**。仕入/売上の in-place 編集は無く、訂正は `cancel_inventory_movement`(inventory/app.py:1452) が**逆仕訳の訂正行(correction)＋`inventory_corrections`** を足す形。
- 送信は `freee_sync_queue`（`UNIQUE(source_type, source_id)`・冪等）→ `send_queue_to_pseudo_freee`(inventory/app.py:1567) が `/api/deals` に POST、`status='sent'`＋`external_accounting_id=pseudo-freee-{id}`。
- **現状の穴**: 送信済み(`sent`)の仕訳を在庫で取り消しても疑似freee へ伝播しない（app.py:1507「sent は触らない」）→ 乖離する。疑似freee 側に同期済み deal の削除/更新 API は無い。

### 設計判断（ユーザ承認＝C案）
- **freee は人が手で触らない**（①-A は不採用）。在庫が唯一の正。
- **「修正」＝「取消＋新規」**（在庫が追記型のため）。作るコアは**取消の伝播**だけ。新規入力は既存の push で足りる。
- **取消は「元仕訳のマイナス（取消仕訳）」を1本 push** する。疑似freee の集計は全て金額ベースの足し算なので、マイナス deal を保存するだけで 仕訳・KPI・BS・PL・残高がすべて自動で相殺される＝**疑似freee は実質無改修**。元仕訳＋取消仕訳の両方が残り**監査証跡**になる。
- 運用は既存と統一：取消すると**取消仕訳が自動でキューに積まれ、送信ボタンで反映**（疑似freee へ送るものは常に明示操作）。

### 実装メモ（次セッション用）
- **在庫 `inventory_dashboard/app.py`**:
  - `enqueue_freee_cancel(conn, org, source_type, source_id)`: `build_freee_payload` を符号反転（details の amount/quantity をマイナス・memo に「取消」）。`freee_sync_queue` に **`source_type='purchase_cancel'/'sale_cancel'`・`source_id=元のid`**（在庫キューの UNIQUE を満たす）・`status='pending'` で INSERT（ON CONFLICT DO UPDATE で冪等）。
  - `cancel_inventory_movement`: 元伝票の queue が `sent` の場合に `enqueue_freee_cancel` を呼ぶ（未送信なら従来どおり `cancelled`）。
  - `send_queue_to_pseudo_freee`: `source_type` が `*_cancel` のとき **base 型(`purchase`/`sale`)へ戻して** POST（疑似freee の source_type CHECK と整合）。`queue_id` が別なので疑似freee 側は新規行として保存（冪等性維持・元仕訳と相殺）。
- **在庫 UI `index_html.py`**: 送信キューに「取消仕訳」と分かる表示（`*_cancel` のラベル）。
- **疑似freee**: 実質無改修。任意で「取消」バッジ（`amount < 0` の deal の見栄え）だけ。
- **テスト**: 在庫＝`enqueue_freee_cancel` のpayload符号反転・`cancel_inventory_movement` が sent のとき取消をキュー投入・`*_cancel`→base マップ（HTTP はモック/分離）。疑似freee＝マイナス金額 deal が KPI/BS/PL を相殺すること。
- **キー衝突注意**: 取消は必ず `*_cancel` 型で積む（`source_id` に correction movement id を使うと purchase.id と数値衝突しうるため、`source_type` を分けるのが安全）。

---

## Phase D — 在庫⇄会計の整合性強化（API連携の完成）

> **状態: 方向性のみ承認（2026-06-24・ユーザ選択=案①）。実装は Phase C を main マージ後に新フェーズとして着手。** 目的は「実務で使える在庫＋会計ソフト」として、**在庫の全取引を会計側が漏れなく忠実に映し、両者が常に一致する**こと。

### 背景（2026-06-24 のユーザ問い合わせで顕在化）
- 在庫(`inventory.db`)と疑似freee(`pseudo_freee.db`)は**別データベース**で、同期するのは「freee送信」した取引だけ。全件ミラーではない。
- 現状の乖離の正体は**バグではなく**「①送信が手動ボタン任せ＝送り忘れ ②取消が伝播していなかった（→Phase C で解消） ③一致を証明する突合が無い ④手動テストの古いデータ滞留」。
- **CSV一括取込の二重計上リスク（Q1）**: `api_import_sales_history`(app.py:2045-2063) は CSV 1 行ごとに `sales`＋`inventory_movements`(`movement_type='sale'`・在庫を減らす) の**両方**に書き、予測(`forecasting/data.py:24` `FROM sales`)もこの sales を読む。つまり **CSV は「予測用シミュレーション」ではなく実在庫元帳そのもの**。A-9 は「クリーンスタート（全消去）→CSV で実運用データ投入」の**置換運用**前提なので、その流れなら二重計上しないが、**既存の実取引にCSVを足すと二重計上**する。区別印は `partner_name='CSV取込'`/`note='CSV取込'`/`external_accounting_status='imported'` のみ。

### 設計判断（ユーザ承認＝案①）
- **2DB＋API連携を維持して強化する**（案②=単一DB統合は採らない）。理由: freee は本来「外部の会計SaaS」で、在庫/EC/POS を **API連携**で繋ぐのが実務そのもの＝本ポートフォリオの主題（freee連携）を最も活かせる。既存 `freee_sync_queue`（`UNIQUE(source_type, source_id)` で冪等）は**教科書的な Outbox パターン**で、それを「自動送信＋取消(Phase C)＋突合」で完成させる王道ストーリー。

### 想定スコープ（着手時に詳細化）
1. **自動・確実な送信**: 仕入/売上の登録時に自動で push（or 既存キューに「未送信を一括送信」＋リトライ）。送り忘れで乖離しない。
2. **取消の伝播**: Phase C で実装済み（reverse-and-repost）。
3. **期末突合/棚卸連携**: Phase B（在庫→freee 期末棚卸API連携）。
4. **突合（照合）ビュー**: 在庫側の集計（売上合計・仕入合計・期末在庫）と疑似freee 側（売上高・仕入高・商品）が一致することを画面で証明（不一致なら差分を表示）。
5. **CSV シミュレーション/実取引台帳の分離（Q1対応）**: 「予測用の需要履歴」と「記帳する実取引」を概念分離。案: (a) CSV は需要履歴専用テーブルへ入れ在庫元帳/会計には流さない、or (b) 実運用データとして扱うなら**置換運用を強制/明示**し会計送信対象外を維持。どちらにするかは着手時にユーザと確定。

### 工数の目安
中程度（既存の queue/Outbox を延長）。案② 統合は書き直しに近く大。
