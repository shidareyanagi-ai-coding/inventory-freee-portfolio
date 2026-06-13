# Deployment Plan Draft

## 目的

このポートフォリオは、GitHub公開とクラウドデプロイを前提に整理します。

最初はローカルSQLiteで動かし、その後PostgreSQLとクラウド環境へ移行します。

## GitHub公開前に必要な整理

- 上位フォルダを作る
- `inventory_dashboard` と `pseudo_freee` を分ける
- ルートREADMEを整える
- `.gitignore` を作る
- DBファイルをGit管理から除外する
- サンプルデータ生成手順をREADMEに書く
- スクリーンショットを `docs/screenshots` に整理する

## .gitignore方針

GitHubに含めないもの:

```text
*.db
__pycache__/
.env
.venv/
node_modules/
```

GitHubに含めるもの:

```text
README.md
ARCHITECTURE.md
DEVELOPMENT_HANDOFF.md
DEPLOYMENT_PLAN.md
docs/
inventory_dashboard/app.py
pseudo_freee/app.py
```

## ローカル起動案

```powershell
# 在庫管理アプリ
cd inventory_dashboard
python app.py

# 疑似freeeアプリ
cd pseudo_freee
python app.py
```

URL:

```text
inventory_dashboard: http://127.0.0.1:8000
pseudo_freee: http://127.0.0.1:8010
```

## 環境変数案

inventory_dashboard:

```text
APP_PORT=8000
DATABASE_URL=sqlite:///inventory.db
PSEUDO_FREEE_API_URL=http://127.0.0.1:8010
```

pseudo_freee:

```text
APP_PORT=8010
DATABASE_URL=sqlite:///pseudo_freee.db
```

## 初期デプロイ案

最初は以下のどちらかが良いです。

### Render

- Pythonアプリをデプロイしやすい
- PostgreSQLを追加しやすい
- ポートフォリオ向き

### Railway

- 複数サービス構成にしやすい
- PostgreSQL追加が簡単
- デモアプリ向き

## 本番寄り構成

```text
GitHub Repository
  ↓
Render / Railway
  ├─ inventory-dashboard service
  ├─ pseudo-freee service
  └─ PostgreSQL
```

## DB移行方針

初期:

- SQLite
- サンプルデータ自動生成
- ローカル動作確認

次段階:

- DBアクセス層を分離
- `DATABASE_URL` で接続先を切り替え
- PostgreSQL対応

本番:

- PostgreSQL
- マイグレーション管理
- バックアップ
- ログ管理

