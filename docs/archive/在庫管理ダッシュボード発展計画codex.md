# 在庫管理ダッシュボード発展計画 codex 改訂版

## Summary

現在の `inventory-freee-portfolio` は、Python標準ライブラリ + SQLite + 手書きHTML/JSで動く、在庫管理・疑似freee連携・簡易予測のMVPです。これを発展させる場合、添付の「在庫予測プロジェクト構想.md」の内容は十分に統合できます。むしろ、既存アプリには在庫移動台帳、仕入・売上履歴、適正在庫シミュレーション、過去デモデータがあるため、需要予測ポートフォリオへ発展させる土台はかなり良いです。

最強構成としては、**Next.js/React + FastAPI + Neon PostgreSQL + Clerk + Python予測モジュール** を推奨します。UI/UXはNext.js/Reactで高め、業務ロジック・API・需要予測はPython/FastAPIで担う分業構成です。これにより、見た目の完成度と、Pythonによるデータ分析・需要予測の実力の両方を示せます。

## Recommended Architecture

| 層 | 推奨 | 役割 |
|---|---|---|
| フロントエンド | **Next.js / React** | ダッシュボード、グラフ、フィルタ、ログイン後UI、予測線表示を担当 |
| バックエンドAPI | **FastAPI** | 在庫、仕入、売上、freee連携キュー、予測結果取得APIを担当 |
| DB | **Neon PostgreSQL** | SQLiteから移行する本格DB。複数ユーザー、同時利用、バックアップに対応 |
| 認証 | **Clerk** | 自前認証を作らず、ログイン、セッション、ユーザー管理、RBACの土台を担当 |
| 予測処理 | **Python modules / batch job** | 移動平均、Prophet、LightGBM、バックテスト、特徴量エンジニアリングを担当 |
| 会計連携 | **pseudo_freee → 将来freee API** | 既存の疑似freee連携を維持し、本物freee OAuth連携へ段階移行 |

この構成の意図は、FastAPIをUI技術として使うことではありません。FastAPIはAPI・業務ロジック・Python予測処理との接続を担い、UI/UXはNext.js/Reactで作ります。

## Technology Choice

### 第一推奨: Neon + Clerk

- NeonはPostgreSQL専用の管理DBとして使う。
- Clerkは認証専用として使う。
- 役割が分かれているため、設計が明確でポートフォリオでも説明しやすい。
- ClerkはNext.jsとの相性が良く、ログインUIや認証状態の扱いがスムーズ。
- Python/FastAPI側ではClerkのJWTを検証し、APIアクセスを保護する。

### 代替案: Supabase一本化

- SupabaseはDB + Auth + Storage + Realtimeを一体で持つため、サービス数を減らしたい場合に有力。
- Supabaseをブラウザから直接使うなら、Row Level Security (RLS) は必須。
- FastAPIを経由してDBにアクセスする構成なら、API側認可を中心にできるが、それでもRLSを補助防衛として使う設計は有効。

### 避けたい構成

- ClerkとSupabase Authを両方使う認証二重化。
- Neon、Clerk、Supabaseを役割整理せず全部入れる構成。
- いきなりNext.jsへ全面移行して、既存Pythonの業務ロジックと予測資産を捨てる構成。

## Key Implementation Changes

### 1. フロントエンド刷新

- 現在の手書きHTML/JSを、段階的にNext.js/Reactへ移行する。
- 在庫一覧、在庫元帳、仕入・売上登録、freee送信キュー、予測ダッシュボードを画面単位で整理する。
- グラフは実績線と予測線を重ねて表示できるようにする。
- UIは「業務アプリ」として、見やすいテーブル、検索、フィルタ、タブ、モーダル、状態バッジを中心に作る。

### 2. FastAPI化

- 既存 `inventory_dashboard/app.py` の業務ロジックはなるべく再利用する。
- `InventoryHandler` のルーティング部分をFastAPIのルータへ移す。
- 在庫、商品、取引先、仕入、売上、在庫元帳、freeeキュー、予測結果をAPIとして分離する。
- `pseudo_freee` も必要に応じてFastAPI化し、将来の本物freee API連携に置き換えやすくする。

### 3. PostgreSQL移行

- SQLiteからNeon PostgreSQLへ移行する。
- DB接続は `DATABASE_URL` で管理する。
- DBアクセス層を分離し、UI/API/業務ロジックにSQLが散らばらないようにする。
- 既存の `AUTOINCREMENT` やSQLite方言をPostgreSQL用DDLへ移植する。
- SKUなどの一意制約は、単純な全体一意ではなく `owner_id + sku` のようなテナント内一意に変更する。

### 4. 認証とマルチテナント

- Clerkでログインを実装する。
- FastAPI側ではClerk JWTを検証し、未認証リクエストを拒否する。
- 全主要テーブルに `owner_id` または `organization_id` を持たせる。
- 全クエリで必ず `owner_id` を条件に含め、他ユーザーのデータを読めないようにする。
- 管理者、担当者、閲覧者の最小RBACを導入する。

### 5. 需要予測の発展

- 既存の `forecast_simulation` はベースライン予測として活かす。
- 新規 `forecasts` テーブルを作り、商品別・日付別・モデル別の予測値を保存する。
- 予測手法は段階導入にする。
  - baseline: 移動平均、前年同月、季節係数
  - Prophet: 季節性とイベント要因の確認
  - LightGBM: 補助金フラグ、曜日、月、祝日、イベント、過去売上ラグなどの特徴量を投入
  - DL系: データ量と必要性がある場合のみ追加
- MAE、MAPE、バックテストを表示し、「単に予測した」ではなく「検証した」状態にする。

### 6. freee連携の扱い

- 当面は既存の `pseudo_freee` を連携デモとして維持する。
- 本物freee OAuth連携は、認証・DB・マルチテナントが安定してから追加する。
- freee送信キューは二重送信防止、失敗時再送、送信前レビューを維持・強化する。
- OAuthトークンやAPIシークレットはDBまたはsecret storeで安全に扱い、ブラウザへ露出させない。

## Security Defaults

- 自前認証は作らず、ClerkまたはSupabase Authに任せる。
- DB接続文字列、Clerk secret、freee tokenなどは環境変数またはsecret storeに置き、Gitへコミットしない。
- DBは原則サーバー側からのみ接続し、ブラウザへDB接続情報を出さない。
- 全APIで認証を必須にする。
- 全主要テーブルで `owner_id` / `organization_id` によるデータ分離を行う。
- IDOR対策として、`product_id`、`source_id`、`queue_id`、`movement_id` などの直接指定APIでは、対象レコードの所有者チェックを必ず行う。
- 認証後も「ログイン済みなら全部見える」にはせず、ロールごとに操作範囲を制限する。
- SQLはパラメータ化クエリまたはORMを使い、文字列連結で組み立てない。
- 公開デモでは、ユーザーごとにサンドボックスデータをseedし、他ユーザーのデモ操作が混ざらないようにする。
- Supabaseを採用する場合、ブラウザ直アクセスを許すテーブルではRLSを必須にする。

## Data Model Additions

- `users` または外部ClerkユーザーIDの対応テーブル。
- `organizations` を使う場合は、組織単位で在庫データを分離する。
- 既存主要テーブルに `owner_id` または `organization_id` を追加する。
- `forecasts` テーブルを追加する。
  - `owner_id`
  - `product_id`
  - `target_date`
  - `model_name`
  - `predicted_quantity`
  - `lower_bound`
  - `upper_bound`
  - `mae`
  - `mape`
  - `created_at`
- 外部要因テーブルを追加する。
  - 補助金フラグ
  - イベント名
  - 対象期間
  - 影響カテゴリ
  - メモ
- 監査ログは初期必須ではないが、更新・削除・送信系操作については将来的に追加する。

## Implementation Phases

### Phase 0: 開発環境整理

- OneDrive外の英数字パスへの移設を検討する。
- `.env.example` を作成し、実値なしで必要な環境変数を整理する。
- 現行SQLite版のテストと画面キャプチャを残し、移行前の基準を作る。

### Phase 1: API分離とFastAPI化

- 既存ロジックを関数単位で整理する。
- FastAPIで商品、仕入、売上、在庫元帳、freeeキューAPIを作る。
- 既存テストをFastAPI用に移植する。

### Phase 2: PostgreSQL移行

- Neon開発DBを用意する。
- PostgreSQL用DDLとマイグレーション方針を作る。
- SQLiteのサンプルデータ生成処理をPostgreSQLでも再現できるようにする。

### Phase 3: 認証とテナント分離

- Clerkを導入する。
- FastAPIでJWT検証を実装する。
- 全主要テーブルに `owner_id` を追加する。
- 全APIで所有者チェックを行い、IDORを塞ぐ。
- 初回ログイン時にユーザー専用のデモデータをseedする。

### Phase 4: Next.js/Reactフロントエンド

- ログイン後の業務ダッシュボードをNext.js/Reactで作る。
- 在庫一覧、元帳、仕入・売上登録、freeeキュー、予測ダッシュボードを統合する。
- Clerkの認証状態を使い、未ログイン時はログインへ誘導する。

### Phase 5: 予測モデル強化

- `forecasts` テーブルを作る。
- baseline予測を保存・表示する。
- ProphetまたはLightGBMを追加し、補助金フラグやカレンダー特徴量を使う。
- バックテストとMAE/MAPEを画面に表示する。

### Phase 6: デプロイとポートフォリオ化

- Render、Railway、またはVercel + APIデプロイ構成を検討する。
- Neon本番DB、Clerk本番キー、環境変数を設定する。
- README、ARCHITECTURE、DEPLOYMENT_PLAN、スクリーンショットを更新する。
- 面接で説明できるように、技術選定理由、セキュリティ設計、予測モデル比較をドキュメント化する。

## Test Plan

- 未ログインでAPIにアクセスした場合、401になること。
- 別ユーザーの `product_id`、`queue_id`、`source_id` を指定しても403または404になること。
- 仕入登録後、在庫移動、在庫元帳、freee送信キューが同じ `owner_id` で整合すること。
- 売上登録時、在庫不足なら登録できないこと。
- freee送信済みキューが重複送信されないこと。
- 予測バッチ実行後、`forecasts` に商品別・日付別・モデル別の予測が保存されること。
- ダッシュボードで実績線と予測線が正しく重なること。
- 2ユーザーでログインし、それぞれ独立したデモデータが表示されること。
- `.env` やAPIキーがGitに含まれていないこと。

## Final Recommendation

最終的なおすすめは、**Next.js/React + FastAPI + Neon PostgreSQL + Clerk + Python予測処理** です。

Claude案の強みである「既存Python資産を活かす」「FastAPIへ現実的に移行する」「マルチテナントとIDOR対策を重視する」方針を採用しつつ、UI/UX面ではNext.js/Reactを使うのが一番バランスが良いです。

この構成なら、ポートフォリオとして以下を同時に示せます。

- 現代的なWeb UIを作れること。
- Pythonで業務ロジックとAPIを設計できること。
- PostgreSQLを使った本格DB設計ができること。
- Clerk等の外部認証を安全に統合できること。
- IDORやマルチテナント分離など、実務的なセキュリティ観点を理解していること。
- 需要予測を、ベースラインからLightGBM等へ段階的に発展させ、評価指標つきで説明できること。

つまり、「見た目の良いダッシュボード」ではなく、**業務理解、会計連携、セキュリティ、DB設計、Python需要予測まで含むフルスタック業務改善アプリ**として見せるのが最も強いです。
