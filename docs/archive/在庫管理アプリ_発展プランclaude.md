# 在庫管理アプリ 発展プラン（統合版 / claude案 × codex案 良いとこ取り）

## Context（なぜこの変更をするのか）

現状の `inventory-freee-portfolio` は、Python標準ライブラリ（`http.server` 手書きサーバ）＋SQLite＋
`app.py`埋め込みの素HTML/JSで動く**単一テナント・認証なし**のアプリ（inventory_dashboard:8000 / pseudo_freee:8010 の2サービス）。
別構想「在庫予測プロジェクト構想.md」が描く**需要予測（ポートフォリオ本丸）＋本格DB＋ログイン認証**を、この既存アプリに統合して発展させる。
スコープは確認済みで **「ポートフォリオ公開用」**（実顧客の機密データは扱わない）。

このファイルは、独立に作られた2つの計画（claude案・codex案）を突き合わせ、**良いとこ取りで統合**したもの。
- **2案が一致した80%**（＝信頼度が高い結論）: 認証=Clerk、DB=Neon（代替Supabase一本）、マルチテナント化が本丸、予測は易→難の段階導入、freeeトークンはサーバ側のみ・暗号化。
- **claude案から採用**: OneDrive外移設、IDOR箇所の具体名指し、訪問者別サンドボックス、既存関数の再利用、スコープ校正（必須/任意の線引き）。
- **codex案から採用**: 軽量RBAC＋監査ログを「見せる機能」として導入、`external_factors`（外部要因）/`order_candidates`（発注候補）テーブルの明示、組織単位（organization_id）テナント。

既存資産（活かす）: 在庫移動台帳は**日次粒度の時系列**、`forecast_simulation`（移動平均×季節係数）で**ベースライン予測が実装済み**、過去24ヶ月の季節性デモデータあり。
→ MDのPhase 2（Prophet→LightGBM→DL）は「ゼロから」ではなく「今ある予測の深掘り」として接続する。

---

## 結論：推奨アーキテクチャ（2案が合意した土台）

| 層 | 推奨 | 内容 |
|---|---|---|
| DB | **Neon (Postgres)** | SQLite→Postgres移行。DBアクセス層を分離し `DATABASE_URL` で切替 |
| 認証 | **Clerk** | 「自前認証を作らない」。サーバ側でClerk JWTをJWKS検証。代替=Supabase Auth |
| テナント | **organization_id ＋ memberships(role)** | 組織単位で全データを分離。最小構成では「1ユーザー=1組織」に縮約可 |
| 権限 | **軽量RBAC（admin / staff / viewer）** | ポートフォリオで“見せられる”最小RBAC。任意だが推奨 |
| 監査 | **audit_logs（軽量）** | 作成・取消・送信などの操作ログ。任意だが推奨 |
| 予測 | **Pythonモジュール/サービス** | baseline(済)→Prophet→LightGBM+特徴量→(任意)DL。同じNeon DBを読み予測線を重ねる |
| freee連携 | 当面は `pseudo_freee` で代替 | 本物freee OAuthは本番化時。トークンは暗号化・サーバ側のみ |
| デプロイ | **Render または Railway** | Python親和。Neon=managed Postgres、Clerk=認証 |

> 代替: **Supabase一本**（DB+認証を集約）。その場合は **RLSに頼らずサーバ側で認可**するか、RLSを使うなら必須でポリシー設計する。
> Clerk と Supabase-RLS を混ぜる場合は **JWT/RLS連携の設計を先に固める**（2案とも一致した注意点）。

---

## スタック決定（唯一残る分岐：狙う職種で決める）

2案が割れた唯一の論点。**この統合版の既定はC（ハイブリッド）**＝「良いとこ取り」の趣旨に最も忠実。時間を絞るならA/Bへ縮約する。

| 案 | 構成 | 向く狙い | トレードオフ |
|---|---|---|---|
| **A. Python継続** | FastAPI主役＋（既存HTML改修 or 軽量React）。認証はClerkホスト画面 or Supabase | **データ/ML職** | 手戻り最小・ML中心。Web見栄えは控えめ |
| **B. Next.js全面** | Next.js/Prisma＋Clerk＋Neon。MD原案どおり | **Web/フルスタック職** | 王道で通じる。動くPython資産を作り直す |
| **C. ハイブリッド（既定）** | **Next.js(Clerk)フロント ＋ FastAPI/Python(ML)バック ＋ 同じNeon DB** | 両取り（ML＋Web双方を見せる） | 信号は最強だが作業量・サービス数が最大 |

判断基準: **狙う職種**（データ/ML→A、Web→B、両方見せたい→C）と**投下できる時間**。
既存が既に2サービス構成なので、Cの「2デプロイ」運用は思想的に無理がない。

---

## セキュリティ方針（「公開用」に校正 ＋ codexの手厚さを軽量導入）

3社（Neon/Clerk/Supabase）ともSOC2等準拠で**選定は妥当**。リスクは**統合の仕方**に出る。

### 必須（公開用でも省略不可）
- **自前認証を作らない**（Clerk/Supabase Authに委譲。パスワード保管・セッション・MFA・CSRFを肩代わり）。
- **マルチテナント化＝本丸**。全ドメインテーブルに `organization_id` を付与し、**全クエリで必ず絞る**。
  現状の **IDOR箇所**を必ず塞ぐ:
  - `product_ledger(conn, product_id)` → `/api/products/<id>/ledger`（任意IDで他人の元帳が引ける）
  - `build_freee_payload` → `/api/freee-preview?source_type&source_id`
  - `cancel_inventory_movement` ほか各 `/api/*` 更新系
- **認証後も「ログイン済みなら全部見える」にしない**（ロール別に操作範囲を分ける／最低でも自組織のみ）。
- **DB接続文字列・APIキー・freeeトークンをブラウザに出さない**（サーバ側＆環境変数のみ）。
- **HTTPS**（プラットフォーム標準で無料）。stdlibサーバ直公開はしない。
- **パラメータ化クエリ**は既存コードで `?` 使用済み=SQLi耐性あり（継続）。
- **シークレットをコミットしない**（`.gitignore` で `.env` 除外済み=◎）。`.env.example` は変数名のみ。
- **デモデータのseed/reset**（公開デモが荒れても復旧可能に）。

### 推奨（公開用なら「見せる機能」として軽量導入：codex採用点）
- **軽量RBAC**（admin/staff/viewer の3ロール、最小構成）。
- **監査ログ**（誰が・いつ・何を：作成/取消/送信）。
- **freee関連の堅牢化**（OAuthトークン暗号化・二重送信防止・再送制御）。二重送信防止は既存実装あり、本物連携時に暗号化保管を追加。

### 後回し可（公開用スコープ）
- SOC2級の本格監査、細粒度RBAC、プラットフォーム標準超の暗号化、ペンテスト、本物freee OAuth（`pseudo_freee` が連携フローを代替済み）。

### 公開デモ向けパターン（claude採用点）
- **サインインごとに自組織サンドボックス**を作り、初回ログイン時にデモデータをseed。
  → マルチテナント／RBACの実演 ＋ 公開デモの荒らし防止を同時に達成。

---

## データモデルの変更

1. **テナント＆権限**（新規）
   - `organizations(id, name, created_at)` … 公開用は「1サインアップ=1組織」。
   - `memberships(id, organization_id, user_id /*Clerk user id*/, role /* admin|staff|viewer */, created_at)`。
2. **全ドメインテーブルに `organization_id`** を追加: `products`/`business_partners`/`purchases`/`sales`/
   `inventory_movements`/`freee_sync_queue`/`inventory_corrections`。
   一意制約はテナント内一意へ: 例 `products.sku UNIQUE` → `UNIQUE(organization_id, sku)`。
3. **予測系（新規）**
   - `forecasts(id, organization_id, product_id, target_date, model_name, predicted_quantity, lower, upper, created_at)`
     … 実績と同じ**日次粒度**。`model_name` で baseline/prophet/lightgbm を切替表示（MDの「手法を易→難で比較」を画面化）。
   - `external_factors(id, organization_id, factor_date, factor_type /* 補助金|イベント|カレンダー等 */, product_id NULL=全体, value, note)`
     … LightGBMの特徴量源（**補助金フラグ・カレンダー要因**＝MDの核心「担当者の頭の中を特徴量に翻訳」）。【codex採用点】
   - `order_candidates(id, organization_id, product_id, suggested_date, recommended_quantity, basis, status, created_at)`
     … **発注候補/月次発注リスト**を履歴として保存（現状は `forecast_simulation` 内で都度計算）。【codex採用点】
4. **監査（新規・任意）**: `audit_logs(id, organization_id, actor_user_id, action, target_type, target_id, detail_json, created_at)`。
5. **Postgres移行**: `SCHEMA_SQL`(SQLite)→Postgres DDL（`AUTOINCREMENT`→`IDENTITY/SERIAL`、`CHECK` は概ね移植）。DBアクセス層で方言差を吸収。

---

## 追加機能：経費キャプチャ（AI証憑入力）　※(あ)で確定＝新FastAPIできれいに実装

`pseudo_freee` の経費入力に、請求書・レシート画像から **AIが伝票の下書きを作る** 機能を追加する。
既存仕様書 `docs/EXPENSE_CAPTURE_FEATURE_SPEC.md` の手順3〜6に相当（手順1〜2＝支払先/勘定科目/税区分の候補マスタ化は実装済み）。

**鉄則（ユーザー要望＝仕様書とも一致）**: AIは **解析してフォームに仮入力するまで**。**「登録」ボタンは人だけが押す**（会計データの自動登録はしない＝責任あるAI活用）。

- **フロー**: 画像アップ(PC)／カメラ撮影(スマホ) → 画像対応AIが解析 → 発生日・支払先・金額・税区分・摘要・勘定科目候補・**読み取り信頼度** を推定 → フォームに下書き反映 → 人が確認・修正 → 登録。
- **スマホ撮影**: `<input type="file" accept="image/*" capture="environment">`（ネイティブアプリ不要）。低信頼度の項目は画面で目立たせる。
- **AIモデル**: 画像が読めるAI（既定は Claude のAPI）を **サーバ側から** 呼ぶ。**APIキーはサーバ側・環境変数のみ**（ブラウザに出さない）。画像1枚ごとに少額の従量課金。
- **実装場所**: 新FastAPIに `POST /api/expense-capture`（画像受信→AI解析→構造化JSONを返す）。FastAPIは `UploadFile` で受信が容易。**古いstdlib版には載せない**。
- **データ（新規 `vouchers` 証憑テーブル）**: `id, organization_id, deal_id(NULL可), file_name, storage_path, mime_type, ai_extracted_json, user_corrected_json, confidence, created_at`。元画像・AI抽出・人修正後を後から比較できる（＝見せ場）。
- **画像保存**: 公開用はオブジェクトストレージ（例: Supabase Storage / Cloudflare R2 / S3互換）へサーバ側経由で保存。DBにはパスのみ。
- **テスト観点**: 画像→AI解析→フォーム反映で止まり **自動登録されない** こと／低信頼度が表示されること／`vouchers` に元画像・AI抽出・人修正後が残ること。

---

## 実装フェーズ（既存ROADMAP / MD Phase2 と対応）

| Phase | 内容 | 対応 |
|---|---|---|
| **0. 環境整備** | プロジェクトを **OneDrive外・英数字パス**（例 `C:\Users\masah\dev\`）へ移設。`.venv`、依存定義 | MD推奨 / 同期事故回避【claude】 |
| **1. スタック確定＋骨組み** | A/B/C を確定し、選んだ構成のスケルトン作成（C: Next.jsフロント＋FastAPIバックの土台） | 本プラン上記分岐 |
| **2. Neon移行** | DBアクセス層分離、`DATABASE_URL` 切替、SQLite→Postgres DDL、seed再現 | ROADMAP Step 3 |
| **3. 認証＋テナント＋RBAC** | Clerk導入・JWT検証、全テーブル`organization_id`、軽量RBAC、IDOR封鎖、監査ログ、初回org seed | ROADMAP Step 8 |
| **4. 予測の深掘り** | `forecasts`/`external_factors`/`order_candidates`、baseline→Prophet→LightGBM(補助金・カレンダー特徴量)→任意DL、MAE/MAPE＋バックテスト、予測線表示 | MD Phase 2 / ROADMAP Step 6 |
| **5. 経費キャプチャ（AI証憑入力）** | pseudo_freee側に 画像アップ/カメラ→AI解析→**下書き反映（登録は人）**、`vouchers`表、低信頼度表示 | EXPENSE_CAPTURE_FEATURE_SPEC.md 手順3〜6 |
| **6. デプロイ** | Render/RailwayへWeb＋(必要なら)予測サービス、Neon接続、Clerk本番キー、READMEとスクショ更新 | DEPLOYMENT_PLAN |

予測の重い学習はオフライン実行→結果を `forecasts` に書き込む構成が公開用に堅実。
予測サービスは既存2サービス思想に合わせ**別FastAPIサービス**として切る選択も可（同じNeon DBを参照）。

---

## 主に触る/新設するファイル

- `inventory_dashboard/app.py` … `InventoryHandler`(stdlib) を撤去。業務ロジック関数
  (`create_purchase`/`create_sale`/`forecast_simulation`/`product_ledger`/`build_freee_payload` 等)は**温存して再利用**。【claude採用点】
- 新規 `db.py`（接続/DDL/アクセス層）、`auth.py`（Clerk JWT検証＋RBACガード）、`requirements.txt`/`pyproject.toml`。
- 新規 `forecasting/`（baseline/prophet/lightgbm、`external_factors` 特徴量、バックテスト、`forecasts`/`order_candidates` 書き込み）。
- 案Cの場合: `web/`（Next.js＋Clerk＋Prisma 等。Prismaは表示/CRUDのみ、予測はPython側）。
- `inventory_dashboard/test_app.py` … 新構成へ更新（テナント分離・RBAC・IDOR防止テスト追加）。
- `pseudo_freee/app.py` … 同様に更新（必要に応じて）。
- ドキュメント: `ARCHITECTURE.md`/`DEPLOYMENT_PLAN.md`/`README.md` を新構成に更新、`docs/` に認証・テナント・予測データ設計を追記。
- `.env.example`（変数名のみ）: `DATABASE_URL`(Neon)、`CLERK_SECRET_KEY`/`CLERK_PUBLISHABLE_KEY`/`CLERK_JWKS_URL`、`PSEUDO_FREEE_API_URL`、`ANTHROPIC_API_KEY`(証憑解析AI)、`STORAGE_*`(証憑画像の保存先)。実値はコミットしない。

---

## 検証方法（統合テストプラン：claude案＋codex案）

1. **認証ガード**: 未認証で 在庫/取引/予測/freeeキュー API にアクセスできないこと（401/403）。【codex】
2. **テナント分離（IDOR）**: 別ユーザー/別組織のデータを閲覧・更新できないこと。相手の `product_id`/`source_id` を直接渡しても 403/404。`test_app.py` に自動化。【両案】
3. **RBAC**: viewer が更新系を実行できないこと、staff/admin の操作範囲が分かれること。
4. **整合性**: 仕入・売上登録後、在庫移動／会計キュー／予測用実績データが整合すること。【codex】
5. **予測**: バッチ実行後、予測が商品別・日付別に `forecasts` へ保存され、ダッシュボードに**実績線＋予測線**が表示されること。`external_factors` を入れた場合に予測が変化すること。バックテストで MAE/MAPE が出ること。【両案】
6. **freee連携回帰**: 送信済みデータが重複送信されず、失敗時は再送可能な状態で残ること。在庫元帳・在庫一覧・シミュレーションが認証導入後も壊れないこと。【両案】
7. **デプロイ**: 公開URL→HTTPSでログイン→各ユーザーにデモseedが入ること。シークレットがリポジトリに無いこと、`.env` 未コミットを確認。

---

## 補足・注意

- **最初にOneDrive外へ移設**（`.db`/`node_modules`/`.venv` の同期ロック事故回避）。
- 認証/DBの最終2択は **フロントの作り方とセットで決定**: React/Next化→**Clerk+Neon**、素HTML/サーバレンダのまま→**Supabase一本**。
- 本物freee OAuthは公開用スコープでは後回し可（`pseudo_freee` が連携デモを担保済み）。トークンを扱う段階で暗号化・監査・再送制御を必須化。
- 軽量RBAC・監査ログは「公開用には必須でない」が、**ポートフォリオでは“セキュリティ意識を見せる機能”として価値が高い**ため、最小構成での導入を推奨。
