# 在庫管理アプリ 発展計画（正本 / EVOLUTION PLAN）

> 📌 **このファイルが、本プロジェクトの「発展方針」の唯一の正本（Single Source of Truth）です。**
> 各 `README` / 要件定義 / 機能仕様より、**全体の進め方・採用スタックは本書を優先**します。
> （元になった検討メモ: claude案・codex案を統合し、その後の決定を反映したもの）

## 確定事項（最新サマリ）

- **スコープ**: ポートフォリオ公開用（実顧客の機密データは扱わない）
- **採用ルート**: **Plan A（シンプル版）を先に完成 → 余力で Plan B（Next.js を上乗せ＝ハイブリッド）**
- **DB**: Neon (Postgres) ／ **認証**: Clerk ／ **テナント**: `organization_id` ＋ 軽量RBAC
- **予測**: **レベル2**（2年・日次の“現実的な合成データ” ＋ 本物の機械学習[Prophet/LightGBM] ＋ MAE/MAPE・バックテスト）
- **データアクセス境界**: **FastAPI が「DB読み書き・業務ロジック・認可」の唯一の主体／フロントは UI と API 呼び出しのみ**
- **追加機能**: 経費キャプチャ（AI証憑入力。AIは下書きまで、登録は人）
- **freee 本物連携**: 後回し可（`pseudo_freee` が連携デモを代替）

---

## Context（なぜ発展させるのか）

現状の `inventory-freee-portfolio` は、Python標準ライブラリ（`http.server` 手書きサーバ）＋SQLite＋
`app.py`埋め込みの素HTML/JSで動く**単一テナント・認証なし**のアプリ（`inventory_dashboard`:8000 / `pseudo_freee`:8010 の2サービス）。
別構想「在庫予測プロジェクト構想」が描く**需要予測（ポートフォリオの本丸）＋本格DB＋ログイン認証**を、この既存アプリに統合して発展させる。

既存資産（活かす）: 在庫移動台帳は**日次粒度の時系列**、`forecast_simulation`（移動平均×季節係数）で**ベースライン予測が実装済み**、過去24ヶ月の季節性デモデータあり。
→ 需要予測の深掘り（Prophet→LightGBM→任意DL）は「ゼロから」ではなく「今ある予測の深掘り」として接続する。

---

## 採用ルート：Plan A → Plan B

| 案 | 構成 | 位置づけ |
|---|---|---|
| **Plan A（先に完成）** | **FastAPI（主役）＋ Neon ＋ Clerk ＋ Python予測**。フロントは既存HTMLを改修して使う | **まずこれを“検証済み”まで完成**。予測・ログイン・DB・AI経費入力が動く芯 |
| **Plan B（余力で）** | Plan A に **Next.js/React フロントを上乗せ**（＝ハイブリッド）。画面をモダンに刷新 | 「最新Web技術も使える」を追加で見せる仕上げ |

**狙い**: 本丸（Python予測）を先に仕上げ、UI刷新は最後の上乗せにする。途中で時間切れでも「動いて評価された需要予測アプリ」が必ず残る。
**料理の味（予測の実力）は A も B も同じ**（どちらも厨房＝Pythonが作る）。違いは画面の華やかさだけ。

---

## データアクセス境界（重要な設計原則）

ハイブリッドで一番事故りやすい所を、最初に固定する。

- **FastAPI が DB への読み書き・業務ロジック・認可の唯一の主体**。
  既存の多テーブル・トランザクション（`create_sale`/`create_purchase`/`cancel_inventory_movement` 等の在庫整合・台帳・freeeキュー）を“唯一の正”として温存する。
- **フロント（Plan B の Next.js）は DB に直接触らない**。表示と API 呼び出しだけ。
- **Prisma は原則不使用**（スキーマ/マイグレーションは Python 側が所有）。OpenAPI から TS 型を生成すればエンドツーエンド型安全（加点）。
  - もし Prisma を見せたい場合のみ「**書き込みは必ず FastAPI／読み取りだけ Prisma**」とし、その際も読み取り側のテナント絞り込み（IDOR対策）をサボらない。
- **認証フロー**: Clerk がフロントでセッション発行 → `Authorization: Bearer` で FastAPI へ転送 → FastAPI が JWKS 検証＋`organization_id` 絞り込み（**認可は FastAPI 単一**）。

---

## 推奨アーキテクチャ

| 層 | 採用 | 内容 |
|---|---|---|
| DB | **Neon (Postgres)** | SQLite→Postgres移行。DBアクセス層を分離し `DATABASE_URL` で切替 |
| 認証 | **Clerk** | 「自前認証を作らない」。サーバ側で Clerk JWT を JWKS 検証 |
| テナント | **organization_id ＋ memberships(role)** | 組織単位で全データ分離。公開用は「1サインアップ=1組織」 |
| 権限 | **軽量RBAC（admin / staff / viewer）** | 見せられる最小RBAC（任意だが推奨） |
| 監査 | **audit_logs（軽量）** | 作成・取消・送信などの操作ログ（任意だが推奨） |
| 予測 | **Python（レベル2）** | 合成データ高度化→baseline→Prophet→LightGBM(+特徴量)→任意DL。同じ Neon を参照 |
| freee連携 | 当面 `pseudo_freee` で代替 | 本物freee OAuthは本番化時。トークンは暗号化・サーバ側のみ |
| デプロイ | **Render または Railway** | Python親和。Neon=managed Postgres、Clerk=認証 |

> 代替: **Supabase一本**（DB+認証を集約）。その場合は RLS に頼らずサーバ側で認可（または RLS を必須設計）。
> Clerk と Supabase-RLS を混ぜる場合は JWT/RLS 連携の設計を先に固める。

---

## セキュリティ方針（「公開用」に校正）

3社（Neon/Clerk/Supabase）とも SOC2 等準拠で**選定は妥当**。リスクは**統合の仕方**に出る。

### 必須（公開用でも省略不可）
- **自前認証を作らない**（Clerk/Supabase Auth に委譲）。
- **マルチテナント化＝本丸**。全ドメインテーブルに `organization_id` を付与し**全クエリで必ず絞る**。
  現状の **IDOR箇所**を必ず塞ぐ:
  - `product_ledger(conn, product_id)` → `/api/products/<id>/ledger`
  - `build_freee_payload` → `/api/freee-preview?source_type&source_id`
  - `cancel_inventory_movement` ほか各 `/api/*` 更新系
- **認証後も「ログイン済みなら全部見える」にしない**（ロール別／最低でも自組織のみ）。
- **DB接続文字列・APIキー・freeeトークン・AIキーをブラウザに出さない**（サーバ側＆環境変数のみ）。
- **HTTPS**（プラットフォーム標準で無料）。stdlibサーバ直公開はしない。
- **パラメータ化クエリ**（既存コードは `?` 使用済み＝SQLi耐性あり）。
- **シークレットをコミットしない**（`.gitignore` で `.env` 除外済み）。`.env.example` は変数名のみ。
- **デモデータの seed/reset**（公開デモが荒れても復旧可能に）。

### 推奨（公開用なら「見せる機能」として軽量導入）
- 軽量RBAC（admin/staff/viewer）／監査ログ（誰が・いつ・何を）／freee関連の堅牢化（トークン暗号化・二重送信防止・再送）。

### 後回し可
- SOC2級の本格監査、細粒度RBAC、本物freee OAuth（`pseudo_freee` が代替済み）。

### 公開デモ向けパターン
- **サインインごとに自組織サンドボックス**を作り、初回ログイン時にデモデータを seed（マルチテナント実演＋荒らし防止）。

---

## データモデルの変更

1. **テナント＆権限**（新規）: `organizations(id, name, created_at)` ／ `memberships(id, organization_id, user_id /*Clerk*/, role, created_at)`。
2. **全ドメインテーブルに `organization_id`** を追加（`products`/`business_partners`/`purchases`/`sales`/`inventory_movements`/`freee_sync_queue`/`inventory_corrections`）。一意制約はテナント内一意へ（例 `UNIQUE(organization_id, sku)`）。
3. **予測系（新規）**
   - `forecasts(id, organization_id, product_id, target_date, model_name, predicted_quantity, lower, upper, created_at)` … 実績と同じ**日次粒度**。`model_name` で baseline/prophet/lightgbm を切替表示。
   - `external_factors(id, organization_id, factor_date, factor_type /* 補助金|イベント|カレンダー等 */, product_id NULL=全体, value, note)` … LightGBM の特徴量源（補助金フラグ・カレンダー要因）。
   - `order_candidates(id, organization_id, product_id, suggested_date, recommended_quantity, basis, status, created_at)` … 発注候補/月次発注リストを履歴保存。
   - `model_evaluations(id, organization_id, model_name, period, mae, mape, created_at)` … バックテストの精度指標（MAE/MAPE は日次予測でなくモデル/期間単位で保持）。
4. **証憑（経費キャプチャ・新規）**: `vouchers(id, organization_id, deal_id NULL可, file_name, storage_path, mime_type, ai_extracted_json, user_corrected_json, confidence, created_at)`。
5. **監査（任意）**: `audit_logs(id, organization_id, actor_user_id, action, target_type, target_id, detail_json, created_at)`。
6. **Postgres移行**: `SCHEMA_SQL`(SQLite)→Postgres DDL（`AUTOINCREMENT`→`IDENTITY/SERIAL`、`CHECK` は概ね移植）。DBアクセス層で方言差を吸収。

---

## 追加機能：経費キャプチャ（AI証憑入力）

`pseudo_freee` の経費入力に、請求書・レシート画像から **AIが伝票の下書きを作る** 機能を追加（既存仕様書 `EXPENSE_CAPTURE_FEATURE_SPEC.md` の手順3〜6に相当。手順1〜2＝候補マスタ化は実装済み）。

**鉄則**: AIは **解析してフォームに仮入力するまで**。**「登録」ボタンは人だけが押す**（会計データの自動登録はしない）。

- **フロー**: 画像アップ(PC)／カメラ撮影(スマホ) → 画像対応AIが解析 → 発生日・支払先・金額・税区分・摘要・勘定科目候補・**信頼度** を推定 → フォームに下書き反映 → 人が確認・修正 → 登録。
- **スマホ**: `<input type="file" accept="image/*" capture="environment">`（ネイティブアプリ不要）。低信頼度の項目は画面で目立たせる。
- **AIモデル**: 画像が読めるAI（既定は Claude のAPI）を **サーバ側から** 呼ぶ。**APIキーはサーバ側・環境変数のみ**。画像1枚ごとに少額の従量課金。
- **実装場所**: 新FastAPIに `POST /api/expense-capture`（画像受信→AI解析→構造化JSONを返す）。**古いstdlib版には載せない**。
- **データ**: 上記 `vouchers` 表（元画像・AI抽出・人修正後・信頼度を保存＝後から見比べられる見せ場）。
- **画像保存**: オブジェクトストレージ（Supabase Storage / Cloudflare R2 / S3互換）にサーバ側経由で保存。DBにはパスのみ。

---

## 完了の定義（Definition of Done）

> 📌 **各フェーズは「私(Claude)の作業」と「ユーザーの作業」の両方が終わり、検証できて初めて「完了」とする。**

- 各フェーズ着手時に、チェックリストを **「私の作業」** と **「ユーザーの作業（外部サービスの契約・課金・接続文字列の取得など、Claudeが代行できないもの）」** に分けて明示する。
- 外部サービス／ユーザー操作が必要な項目は **ハードゲート**。ローカルの代替（例: 使い捨てDocker Postgres）で済ませて先に進めない。必要なら一度止めて知らせる。
- 途中段階は「**コード完了**」「**ローカル検証済**」など、**「完了」とは別の語**で報告する。
- 完了報告の直前に「次フェーズが前提とするものは実在するか」を1つ確認する。
- （経緯: A-2 で、実装＋ローカルPostgres検証が済んだ時点で「完了」と報告したが、計画に明記された**実Neon接続が未了**だった反省から、このルールを明文化した。）

---

## 実装フェーズ

### Plan A（先に完成させる芯）
| Phase | 内容 |
|---|---|
| **A-0. 環境整備** ✅ | プロジェクトを **OneDrive外・英数字パス**（例 `C:\Users\masah\dev\`）へ移設。`.venv`・依存定義・`.env.example` |
| **A-1. FastAPI化** ✅ | `InventoryHandler`(stdlib) を撤去し FastAPI ルータへ。業務ロジック関数は**温存して再利用**。既存テスト移植 |
| **A-2. Neon移行** ✅ | **完了（実Neon接続・検証済み）**。DBアクセス層を `db.py` に分離、`DATABASE_URL` で SQLite⇄Postgres 切替、SQLite→Postgres DDL、seed再現。ORMは不採用（薄い手書きアダプタ）。検証: 実Neon（PostgreSQL 16.14 / Singapore）へ接続→スキーマ作成＋seed投入→全テスト 41 passed（SQLite 28＋Neon上 Postgres 13 / `test_postgres.py`）→ダッシュボードが実Neonデータを正しく表示。`inventory_dashboard` が対象（`pseudo_freee` のPostgres化はA-3以降の検討事項） |
| **A-3. 認証＋テナント＋RBAC** ✅ | **完了（実Clerk＋実Neon検証済み）**。Clerk JWT を JWKS(RS256) で検証（`auth.py`）、全ドメインテーブルに `organization_id`、新規 `organizations`/`memberships`/`audit_logs`、テナント内一意、軽量RBAC（admin/staff/viewer）、IDOR封鎖（別テナントの id は 404）、監査ログ、初回ログインで自組織サンドボックス seed。dev モード（`AUTH_DEV_MODE`）でClerk無しのローカル/テストも可。**前提作業（テスト用DBを本番Neonと分離）も実施**: Neon の**テスト用ブランチ**（`a3-test`）を作成し、`test_postgres.py` は `PYTEST_ALLOW_DB_RESET=1` の明示時のみ DROP 実行（本番誤爆防止）。検証: SQLite 45＋Neonテストブランチ上 Postgres 14 = **59 passed**、実 Clerk でサインイン→JWT検証→自組織サンドボックスにデモ seed→ダッシュボード表示まで確認。commit 48850f1・530595a（main）|
| **A-4. 予測レベル2** ✅ | **完了**（ローカルSQLite＋実Neon(a3-test)検証済・mainマージ済 3d51599）。合成データを2年・日次の“現実的”版に作り直し（トレンド＋週次/月次季節＋補助金/キャンペーンのスパイク＋ノイズ、`external_factors` 記録）、`forecasting/` パッケージに **baseline / SARIMA(statsmodels) / LightGBM(分位点回帰＋補助金/カレンダー特徴量)** をモデルレジストリ（遅延import・依存無は自動skip）として実装。ホールドアウト・バックテストで MAE/MAPE をモデル比較（model_evaluations）、`forecasts`/`order_candidates` 保存、Chart.js で実績線＋予測線＋80%信頼区間を表示。新API: `POST /api/forecast/run`(admin/staff)・`GET /api/forecast/{series,models,evaluations,order-candidates}`（テナント絞り込み・IDOR404）。検証: SQLite **60 passed**（既存45＋新15: テナント分離・予測保存・MAE/MAPE・RBAC・回帰）、ダッシュボードで実績×予測を目視、CLI `python -m forecasting.run` 動作。**正本からの差分（決定）**: Prophet は Windows+Py3.11 の導入安定性の理由で **statsmodels(SARIMA/ETS) に差し替え**／**任意DL は本フェーズ見送り**。**実Neon検証済**: a3-test ブランチで `test_postgres.py` **16 passed**（予測系4テーブルのPG DDL＋`run_forecast` on Postgres＋日次seed再現を含む。`PYTEST_ALLOW_DB_RESET=1`・本番ブランチには向けず）。次は A-5（経費キャプチャ）。 |
| **A-5. 経費キャプチャ** | `POST /api/expense-capture`、画像アップ/カメラ→AI解析→**下書き反映（登録は人）**、`vouchers`表、低信頼度表示 |
| **A-6. デプロイ** | Render/Railway へ、Neon接続、Clerk本番キー、README・スクショ更新 |

→ **ここまでで Plan A 完成** = 動いて検証された需要予測＋ログイン＋本格DB＋AI経費入力。

### Plan B（余力で上乗せ）
| Phase | 内容 |
|---|---|
| **B-7. Next.jsフロント** | ログイン後の業務ダッシュボードを Next.js/React で刷新。Clerk連携、FastAPI を API として呼ぶ（データアクセス境界どおり）。実績線＋予測線、検索/フィルタ/モーダル等 |

→ ハイブリッド完成。

---

## 主に触る/新設するファイル

- `inventory_dashboard/app.py` … `InventoryHandler`(stdlib) を撤去。業務ロジック関数（`create_purchase`/`create_sale`/`forecast_simulation`/`product_ledger`/`build_freee_payload` 等）は**温存・再利用**。
- 新規 `db.py`（接続/DDL/アクセス層）、`auth.py`（Clerk JWT検証＋RBACガード）、`requirements.txt`/`pyproject.toml`。
- 新規 `forecasting/`（合成データ生成、baseline/prophet/lightgbm、`external_factors` 特徴量、バックテスト、`forecasts`/`order_candidates`/`model_evaluations` 書き込み）。
- 新規 `ai_capture.py`（証憑画像→AI→構造化JSON）＋ `pseudo_freee` 側の `POST /api/expense-capture`。
- Plan B のみ: `web/`（Next.js＋Clerk。Prisma原則不使用）。
- `inventory_dashboard/test_app.py` … 新構成へ更新（テナント分離・RBAC・IDOR防止・経費キャプチャの自動登録防止テスト）。
- ドキュメント: 各 README / 要件定義 / 機能仕様は本書（正本）を指すポインタ運用。
- `.env.example`（変数名のみ）: `DATABASE_URL`(Neon)、`CLERK_*`、`PSEUDO_FREEE_API_URL`、`ANTHROPIC_API_KEY`(証憑解析AI)、`STORAGE_*`(証憑画像)。実値はコミットしない。

---

## 検証方法（統合テストプラン）

> ⚠️ **テストDBの分離（A-2以降の必須ルール）**: `inventory_dashboard/test_postgres.py` は対象DBのテーブルを **DROP→再作成** する。`DATABASE_URL` を本番Neon（実データが入るブランチ）に向けたまま `pytest` を実行すると**データが消える**。
> - 既定: テストは **SQLite**（`DATABASE_URL` 未設定）で走り、Postgres検証は**使い捨て環境**で行う。
> - 実Postgresで検証したいときは、**本番とは別の `DATABASE_URL`**（Neonの**テスト用ブランチ**〔無料プランで10ブランチ〕／別テストDB／使い捨てDocker Postgres）に向ける。
> - A-3以降で本物のユーザー/組織データを扱い始めたら、このルールを破らないこと（CIでも本番URLをテストに渡さない）。

1. **認証ガード**: 未認証で 在庫/取引/予測/freeeキュー API にアクセス不可（401/403）。
2. **テナント分離（IDOR）**: 別ユーザー/別組織の `product_id`/`source_id` を直接渡しても 403/404。`test_app.py` に自動化。
3. **RBAC**: viewer は更新系不可、staff/admin の範囲が分かれる。
4. **整合性**: 仕入・売上登録後、在庫移動／会計キュー／予測用実績が整合。
5. **予測**: バッチ後 `forecasts` に商品別・日付別・モデル別で保存、ダッシュボードに**実績線＋予測線**、`external_factors` を入れると予測が変化、バックテストで MAE/MAPE が出る。
6. **経費キャプチャ**: 画像→AI解析→フォーム反映で止まり**自動登録されない**、低信頼度表示、`vouchers` に元画像・AI抽出・人修正後が残る。
7. **freee連携回帰**: 送信済みは重複送信されず、失敗時は再送可能。在庫元帳・一覧・シミュレーションが認証導入後も壊れない。
8. **デプロイ**: 公開URL→HTTPSでログイン→各ユーザーにデモseed、シークレットがリポジトリに無い。

---

## 補足・注意

- **最初に OneDrive外へ移設**（`.db`/`node_modules`/`.venv` の同期ロック事故回避）。正本を**リポジトリ内**に置いたので、移設時も計画が一緒に移動する。
- 認証/DBは Plan A では Clerk+Neon を基本。フロントを Next.js 化する Plan B で Clerk の真価が出る。
- 本物freee OAuth は公開用スコープでは後回し可。扱う段階で暗号化・監査・再送を必須化。
