# 在庫管理ダッシュボード

> 📌 **発展方針の正本は [`../docs/EVOLUTION_PLAN.md`](../docs/EVOLUTION_PLAN.md) です。** 全体の進め方・採用スタックは正本を優先してください。

freee連携を第2ステップで追加しやすいように、仕入・売上を明細単位で保持する在庫管理ダッシュボードです。

> 🛠 **A-1（FastAPI化）完了**: 旧 `http.server`（`InventoryHandler`）を撤去し、Web層を **FastAPI + Uvicorn** へ移行しました。業務ロジック関数（`create_purchase` / `forecast_simulation` 等）はそのまま再利用しています。
>
> 🛠 **A-2（Neon移行）完了**: DBアクセス層を [`db.py`](db.py) に分離し、`DATABASE_URL` で **SQLite ⇄ Postgres（Neon / psycopg3）** を切替えられるようにしました。業務ロジックは方言を意識せず常に `?` で SQL を書き、方言差（`?`/`%s`・`lastrowid`/`RETURNING`・`strftime`/`EXTRACT`・DDL）は `db.py` が吸収します。

## セットアップ

依存はリポジトリ直下の `requirements.txt` にまとまっています（バージョン固定は `requirements.lock.txt`）。

```powershell
# リポジトリ直下で
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 起動方法

```powershell
python app.py
```

開発時にホットリロードしたい場合は Uvicorn を直接起動します。

```powershell
uvicorn app:app --reload
```

ブラウザで以下を開きます。

```text
http://127.0.0.1:8000        # ダッシュボード画面
http://127.0.0.1:8000/docs   # FastAPI 自動生成の API ドキュメント (Swagger UI)
```

### DB の切替（SQLite / Postgres）

`DATABASE_URL` 環境変数で接続先を切替えます（`.env` に書くと `python-dotenv` が読み込みます）。

```powershell
# 既定: 未設定なら SQLite（inventory.db を自動作成）
python app.py

# Postgres（Neon 等）を使う場合
$env:DATABASE_URL = "postgresql://USER:PASSWORD@HOST/DBNAME?sslmode=require"
python app.py   # 起動時にスキーマDDLを流し、空ならデモデータを seed
```

どちらのバックエンドでも、起動時にテーブル作成とデモデータ投入（seed）が再現されます。

## 実装済み

- 商品マスタ
- 取引先マスタ
- 仕入明細登録
- 売上明細登録
- 在庫移動台帳による現在庫計算
- 在庫不足時の売上登録ブロック
- 発注点・安全在庫によるステータス表示
- freee送信用キュー
- freee送信前レビュー用の中間JSON
- 送信済み・失敗・再送待ちに対応できるステータス項目

## 取引先マスタ

仕入先・得意先は `partners` テーブルで管理します。

明細登録画面では、仕入先・得意先を選択式にしています。必要に応じて、その場で新しい取引先を追加できます。

設計上のポイント:

- `partners.partner_type` は `supplier` / `customer` / `both` を持ちます。
- `purchases` / `sales` には `partner_id` と `partner_name` の両方を保存します。
- `partner_name` は登録時点の控えです。後から取引先マスタ名を変更しても、過去帳簿が勝手に変わらないようにします。
- `partners.freee_partner_id` は将来freee側の取引先IDを保存するためのマッピング欄です。
- freee送信用JSONには `partner_master_id` と `freee_partner_id` を含められるため、疑似freee連携や本物freee API連携へ拡張しやすくなります。

## 現在の開発ステータス

現在は第1ステップの実務ベースMVPを実装済みです。

全体計画と現在地は [ROADMAP.md](ROADMAP.md) を参照してください。

## DB方針

A-2 でDBアクセス層を `db.py` に分離し、`DATABASE_URL` で **SQLite ⇄ Postgres** を切替えられる構成になりました。

- **開発・テストの既定**: SQLite。別途DBサーバー不要で、`python app.py` 起動時に `inventory.db` が自動作成されます。
- **本番運用**: Neon（Postgres）。`DATABASE_URL` を Postgres URL にするだけで切替わります。
- **方言差の吸収**: スキーマDDL・採番（`RETURNING id`）・月抽出（`EXTRACT`）などの差は `db.py` が一手に引き受け、業務ロジック（`app.py`）は方言非依存です。
- **採用判断**: ORM（SQLAlchemy）は使わず、薄い手書きアダプタにしています。依存が軽く、面接などで挙動を説明しやすいことを優先しました。

## 関連ドキュメント

- [DESIGN_HANDOFF.md](DESIGN_HANDOFF.md): デザインAIへ渡すための画面改善用受け渡し資料
- [FREEE_INTEGRATION_PLAN.md](FREEE_INTEGRATION_PLAN.md): 疑似freeeデモと将来のfreee API連携計画

## freee連携の考え方

第1ステップではfreee API通信は行いません。仕入・売上登録時に `freee_sync_queue` へ送信待ちデータを保存します。

第2ステップでは、OAuth2.0認可、事業所選択、勘定科目・税区分・取引先・品目・部門マッピングを追加し、キューのデータをfreee会計APIへ送信します。

## テスト

```powershell
python -m pytest -q        # 既定（SQLite）: 28 passed, 13 skipped
```

- `test_app.py`: 業務ロジック関数を直接呼ぶテスト（在庫整合・freeeキュー・予測など / SQLite）。
- `test_api.py`: FastAPI の `TestClient` 経由でルーティング・ステータスコード・`{"error": ...}` 整形を検証（SQLite）。
- `test_postgres.py`: **Postgres バックエンドのスモークテスト**。`DATABASE_URL` が postgres を指すときだけ実行（未設定ならスキップ）。

SQLite 側のテストは `DATABASE_URL` が設定された環境でも必ずローカル SQLite を使うよう隔離しています。Postgres 側は throwaway コンテナで検証できます。

```powershell
# Postgres も含めて検証する例（要 Docker）
docker run -d --name a2pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:16
$env:DATABASE_URL = "postgresql://postgres:pw@127.0.0.1:55432/postgres"
python -m pytest -q        # 41 passed（SQLite 28 + Postgres 13）
docker rm -f a2pg
```
