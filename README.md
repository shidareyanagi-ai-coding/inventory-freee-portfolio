# 在庫管理 × 需要予測 × AI証憑入力 ダッシュボード

小規模EC・中小企業の仕入担当者を想定した、**在庫管理＋AI需要予測＋AI証憑入力**の業務アプリです。
仕入・売上の登録から、適正在庫の判断、会計（freee）連携用データの作成までを一気通貫でデモできます。

## 🚀 ライブデモ

**👉 https://inventory-dashboard-61w8.onrender.com**

- サインイン（Clerk）後、**あなた専用のデモデータ（商品・2年分の販売履歴）が自動投入**され、すぐ触れます。
- 無料ホスティング（Render）のため、**初回アクセスは起動に30〜60秒**かかることがあります（スリープからの復帰）。
- レシート/請求書の**AI読み取り**は、既定では無料のサンプル動作です。本物のAIで試したい場合は、画面の「AI設定」欄に**自分のAnthropic APIキー**を貼ると有効になります（→ [各自APIキー方式](#-各自apiキー方式byo-key)）。

## 📸 スクリーンショット

| ダッシュボード | 適正在庫シミュレーション（AI予測） |
|---|---|
| ![dashboard](docs/screenshots/dashboard-overview.png) | ![forecast](docs/screenshots/forecast-simulation.png) |

> 画像は随時更新します。全画面版は [`docs/screenshots/`](docs/screenshots/) にあります。

## ✨ 主な機能

| 機能 | 内容 |
|---|---|
| 在庫管理 | 商品マスタ・取引先マスタ・仕入/売上登録・商品別在庫元帳・取消/訂正履歴 |
| 適正在庫シミュレーション | 現在在庫・必要在庫・今すぐ発注量・月末判定を一覧表示（**AI予測ベース**） |
| 需要予測（レベル2） | 3モデル（ベースライン/SARIMA/LightGBM）を**バックテスト(MAE/MAPE)で比較し、商品ごとに最良モデルを自動採用**。実績線＋予測線＋信頼区間(80%)をグラフ表示 |
| AI証憑入力 | 仕入/売上の請求書画像 → Claude vision が下書きを生成 → 人が確認して登録（**自動登録はしない**） |
| マルチテナント認証 | Clerk による組織単位のデータ分離（他テナントのデータは見えない設計） |
| freee連携デモ | 送信待ちキュー・送信前レビューJSON（疑似freeeアプリへ連携） |

## 🛠 技術スタック

| 領域 | 採用技術 |
|---|---|
| 言語 / フレームワーク | Python 3.11 / FastAPI + Uvicorn |
| データベース | **Neon (PostgreSQL)** ／ ローカルは SQLite（`DATABASE_URL` で自動切替） |
| 認証 | **Clerk**（JWT を JWKS(RS256) で検証・マルチテナント） |
| 画像ストレージ | **Cloudflare R2**（S3互換）／ 未設定時はローカルフォルダ（`STORAGE_*` で切替） |
| AI（証憑読み取り） | **Anthropic Claude**（vision・structured outputs） |
| 需要予測 | pandas / NumPy / scikit-learn / **LightGBM** / statsmodels(SARIMA) |
| ホスティング | **Render**（Blueprint `render.yaml`） |

## 🧩 アーキテクチャ

```text
            ┌──────────────────────────────┐
            │  ブラウザ（仕入担当者）         │
            └──────────────┬───────────────┘
                           │  Clerk でサインイン
                           ▼
        ┌──────────────────────────────────────┐
        │  在庫アプリ  (FastAPI / Render)         │
        │  ・在庫管理 / 需要予測 / AI証憑入力      │
        └───┬───────────────┬──────────────┬────┘
            │ 文字・数値      │ 画像ファイル   │ AI解析（任意）
            ▼               ▼              ▼
      Neon (Postgres)   Cloudflare R2   Anthropic Claude
      台帳データ          証憑画像        ※利用者のキーで都度実行
```

役割が違う3つの外部サービス（台帳=Neon／倉庫=R2／認証=Clerk）を、それぞれ環境変数で差し替え可能に設計しています。

## 🔑 各自APIキー方式（BYO-key）

公開デモでも**運営者のAI利用料が増えない**よう、AIキーは利用者が持ち込む方式にしています。

- 既定は**AIオフ＝決定的なサンプル動作**（誰でも無料で一通り試せる）。
- 利用者が画面で**自分のAnthropicキーを貼る**と本物のAI解析が有効になる。
- そのキーは**ブラウザにのみ保存**し、解析の**都度だけサーバへ送信**、**サーバ・DB・ログには一切保存しない**。

設計の核は `inventory_dashboard/ai_capture.py`（リクエスト毎にキーを受け取り、無ければスタブにフォールバック）。

## 💻 ローカルでの動かし方

```bash
git clone https://github.com/87yoko-ai-engineer/inventory-freee-portfolio.git
cd inventory-freee-portfolio
python -m venv .venv
# Windows: .venv\Scripts\activate   /   Mac・Linux: source .venv/bin/activate
pip install -r requirements.txt

# 環境変数（任意。未設定でも SQLite + 開発ログイン + スタブAI で動く）
cp .env.example .env   # Windows: Copy-Item .env.example .env

cd inventory_dashboard
python app.py          # → http://127.0.0.1:8000
```

- `DATABASE_URL` 未設定なら **SQLite**、`AUTH_DEV_MODE=true` なら Clerk 無しの**開発ログイン**で動きます。
- テスト: `pytest`（`inventory_dashboard/` 配下。SQLite で実行）。

## 📌 補足・既知の制約

- **疑似freeeアプリ（`pseudo_freee/`）は現在ローカル専用**（Python標準サーバ＋SQLite）で、今回のクラウド公開には含めていません。「freee送信」はデモ上、相手未公開のため穏当にエラー表示されます。
- Clerk は **開発インスタンス**（テストキー）を利用しています（本番インスタンスは独自ドメインが必要なため将来対応）。
- Render 無料枠のため、15分アクセスが無いとスリープ → 次アクセスで数十秒の起動待ちが発生します。

## 📚 ドキュメント

| 資料 | 内容 |
|---|---|
| [`docs/EVOLUTION_PLAN.md`](docs/EVOLUTION_PLAN.md) | 開発計画・採用スタックの検討記録 |
| [`docs/FREEE_INTEGRATION_PLAN.md`](docs/FREEE_INTEGRATION_PLAN.md) | freee連携の設計 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | 構成メモ |
| [`inventory_dashboard/ROADMAP.md`](inventory_dashboard/ROADMAP.md) | 機能ロードマップ |
