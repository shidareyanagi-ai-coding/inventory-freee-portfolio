# 疑似freee 永続化 要件定義書（A-8）

> 📌 関連文書: 全体方針の正本 [`EVOLUTION_PLAN.md`](EVOLUTION_PLAN.md) ／ 機能要件 [`PSEUDO_FREEE_REQUIREMENTS.md`](PSEUDO_FREEE_REQUIREMENTS.md)。
> 本書は「公開した疑似freee のデータ（取引・レシート画像）が消えないようにする」ための要件を定義する。

---

## 0. なぜこの文書をつくるか（一言で）

公開中の疑似freee は、登録したレシートや取引が **再デプロイ・スリープ復帰のたびに消える**。
これを **在庫ダッシュボードと同じ堅牢さ（外部DB＋外部画像ストレージ）** に引き上げ、
登録したデータが永続的に残るようにする。本書はその「あるべき仕様」と「やること」を定義する。

---

## 1. 背景・課題

### 1.1 「保存先は2階建て」という整理（重要）

レシートを保存するには、**性質の違う2つの置き場所**が必要になる。

| 何を保存するか | 置き場所の種類 | 永続にする選択肢 |
|---|---|---|
| ① 取引・レシートの「情報」（日付・金額・勘定科目・摘要など） | データベース | **Neon（Postgres）** ＝外部の永続DB |
| ② レシートの「画像そのもの」 | オブジェクトストレージ | **R2（Cloudflare）** ＝外部の永続ストレージ |

- 画像はDBに詰め込まず、専用のオブジェクトストレージ（R2）に置くのが定石（DBは肥大化させない）。
- 「永続にしたいなら Neon と R2 を入れる」が正しい。Neon は“簡易”ではなく“しっかりした永続DB”である。

### 1.2 現状の問題

- 在庫ダッシュボード = ① Neon ＋ ② R2 の**両方**を使う → データが残る。
- 公開中の疑似freee = ① も ② も使っていない（① コンテナ内 SQLite ファイル／② コンテナ内フォルダ）
  → Render 無料枠のディスクは揮発するため、**両方ともコンテナ入れ替えで全消去**される。
- 起動時 `init_db()` は**マスタ（取引先・勘定科目・税区分）だけ**再投入するため、取引・レシートは毎回0件に戻る。

> 補足: ローカル開発機の `pseudo_freee/pseudo_freee.db` に登録した分は手元に残っている（公開版とは別物）。
> 公開版（Render）だけが揮発する。

---

## 2. 目的・ゴール（受け入れ条件サマリ）

公開中の疑似freee で、以下が成り立つこと。

1. 登録したレシート（**画像＋仕訳情報**）が、**再デプロイ・スリープ復帰の後も残る**。
2. 在庫アプリからの「freee送信」で届いた取引も残る。
3. 在庫ダッシュボードと**同じ堅牢さ**（外部Postgres＋外部オブジェクトストレージ）になる。
4. 既存のローカル開発体験（SQLite＋ローカルフォルダで動く）は**壊さない**＝env で自動切替。

---

## 3. 現状アーキテクチャ（As-Is）

| 層 | 疑似freee（現状） | 在庫ダッシュボード（参考＝あるべき姿） |
|---|---|---|
| 言語/構成 | Python 標準ライブラリ `http.server` | FastAPI（uvicorn） |
| データ | **SQLite**（`pseudo_freee.db`・コンテナ内ファイル） | **Postgres/SQLite 切替**（`db.py`・`DATABASE_URL` で判定） |
| 画像 | **ローカルフォルダ**（`voucher_store/`） | **ローカル/R2 切替**（`storage.py`・`STORAGE_*` で判定） |
| 永続性 | **なし（揮発）** | **あり**（Neon＋R2） |
| 認証 | Clerk（在庫と共有・実装済） | Clerk |

### 3.1 現状のデータモデル（テーブル）

- `deals`（取引ヘッダ） / `deal_lines`（明細）
- マスタ: `payees`（取引先）/ `account_items`（勘定科目）/ `tax_categories`（税区分）ほか
- `pseudo_freee_vouchers`（証憑＝レシート画像のメタ。`storage_path` 等で画像を参照）

### 3.2 現状コードの SQLite 依存ポイント（移行で対応が要る箇所）

- 接続: `sqlite3.connect` / `sqlite3.Row` / `conn.executescript(SCHEMA_SQL)`
- プレースホルダ: `?`（SQLite方言）
- 自動採番: `INTEGER PRIMARY KEY AUTOINCREMENT` ＋ `cursor.lastrowid`
- 重複回避: `INSERT OR IGNORE` 等の SQLite 構文
- 画像: `store_voucher_image()` が `VOUCHER_DIR` に直接ファイル書き込み

---

## 4. 目標アーキテクチャ（To-Be）

```text
[ブラウザ] --Clerk--> 疑似freee (Render Web Service)
                         |
        ┌────────────────┴─────────────────┐
        | ① 取引・レシート情報              | ② レシート画像
        v                                   v
   Neon (Postgres)                      Cloudflare R2
   = 疑似freee 専用DB(在庫とは分離)       = S3互換オブジェクトストレージ
```

- env が揃えば外部（Neon / R2）、揃わなければ従来どおりローカル（SQLite / フォルダ）。
  → **本番＝永続、ローカル＝手軽**を自動で両立（在庫アプリと同じ方針）。

---

## 5. 機能要件（永続化に関わるもの）

| ID | 要件 |
|---|---|
| FR-1 | 取引（在庫連携・手入力経費）を Postgres に保存し、再起動後も一覧/詳細/KPI/月次に反映される。 |
| FR-2 | レシート画像を R2 に保存し、再起動後も `GET /api/vouchers/{id}/image` で取得できる。 |
| FR-3 | 証憑の登録・削除・重複検知（content_hash）が Postgres 上でも従来どおり動く。 |
| FR-4 | `POST /api/deals`（在庫からの受信）・二重送信防止が Postgres 上でも従来どおり動く。 |
| FR-5 | マスタ（取引先・勘定科目・税区分）の再シードは「無ければ入れる」冪等動作を維持（既存値は上書きしない）。 |
| FR-6 | ローカル（env 未設定）では SQLite ＋ ローカルフォルダで従来どおり動く（開発体験を壊さない）。 |

---

## 6. 非機能要件

| ID | 区分 | 要件 |
|---|---|---|
| NFR-1 | 永続性 | 本番のデータ（取引・画像）はサービス再デプロイ／スリープ復帰で消えない。 |
| NFR-2 | 分離 | 疑似freee のDBは**在庫アプリとは別のデータベース**にする（別システムのデータを混ぜない）。R2 はバケット共有可だがキー接頭辞 `pseudo-freee/` で分離。 |
| NFR-3 | セキュリティ | 接続文字列・ストレージ鍵は**環境変数のみ**。コード/Git/ログに残さない（既存方針を踏襲）。 |
| NFR-4 | 互換性 | 既存の API・画面・テストの「契約」を変えない（保存先だけ差し替える）。 |
| NFR-5 | 独立性 | 疑似freee は単体で動く構成を保つ（在庫の `db.py`/`storage.py` を **import せず**、同じパターンで独立移植する）。 |
| NFR-6 | 可逆性 | env を外せばローカルSQLiteに戻る＝安全に検証・ロールバックできる。 |

---

## 7. データ設計（SQLite → Postgres 方言差の吸収）

疑似freee 用の薄い DB アダプタ `pseudo_freee/db.py`（新設）を**在庫 `db.py` と同じ考え方**で用意し、以下を吸収する。

| 項目 | SQLite | Postgres | 対応方針 |
|---|---|---|---|
| プレースホルダ | `?` | `%s` | アダプタで変換 or 共通化 |
| 自動採番PK | `INTEGER PRIMARY KEY AUTOINCREMENT` | `GENERATED ... IDENTITY` / `SERIAL` | スキーマDDLを方言別に持つ |
| 採番された値の取得 | `cursor.lastrowid` | `RETURNING id` | アダプタで吸収 |
| 複数文DDL | `executescript` | 非対応 | 文を分割実行 or マイグレーション関数化 |
| 行アクセス | `sqlite3.Row` | `RealDictCursor` 等 | dict 行に統一 |
| 重複回避 | `INSERT OR IGNORE` | `ON CONFLICT DO NOTHING` | 方言別に発行 |
| 真偽値・日付 | 緩い | 型に厳密 | 既存値の型を点検 |

> 既存テーブル（deals/deal_lines/各マスタ/pseudo_freee_vouchers）の**論理設計は変えない**。物理的な置き場所と方言だけを変える。

---

## 8. インフラ要件（🧑 ユーザ操作が必要なもの）

### 8.1 Neon（① データ用）

- 疑似freee 専用の **Neon データベース**を1つ用意（在庫とは分離。新規 Neon プロジェクト or 別データベース推奨）。
- 接続文字列を Render の `pseudo-freee` サービスに `DATABASE_URL` として設定（`?sslmode=require` 付き）。

### 8.2 R2（② 画像用）※ 接頭辞分離で確定

- **既存 R2 バケット（`inventory-vouchers`）を流用し、疑似freee の画像キー先頭に `pseudo-freee/` を付けて分離**する（バケット・鍵を新規作成しないため、ユーザ操作が最小）。
  - 例: 在庫 `org-1/9f8e..._請求書.jpg` ／ 疑似freee `pseudo-freee/a1b2..._レシート.jpg`
  - 実装側で全キーに接頭辞を付与（`pseudo_freee/storage.py` 経由で一元化）。在庫の画像とは混ざらない。
- Render の `pseudo-freee` サービスに、在庫と**同じ R2 の値**を設定:
  `STORAGE_ENDPOINT` / `STORAGE_BUCKET`（=inventory-vouchers）/ `STORAGE_ACCESS_KEY_ID` / `STORAGE_SECRET_ACCESS_KEY` / `STORAGE_REGION=auto`

### 8.3 環境変数 一覧（pseudo-freee サービス・最終形）

| 変数 | 用途 | 状態 |
|---|---|---|
| APP_ENV=production | Clerk必須化 | 設定済 |
| CLERK_PUBLISHABLE_KEY / CLERK_ISSUER | 同じログイン | 設定済 |
| INVENTORY_APP_URL | 入口リンク | 設定済 |
| **DATABASE_URL** | **① Neon（今回追加）** | 未 |
| **STORAGE_ENDPOINT / STORAGE_BUCKET / STORAGE_ACCESS_KEY_ID / STORAGE_SECRET_ACCESS_KEY / STORAGE_REGION** | **② R2（今回追加）** | 未 |

---

## 9. 作業計画（担当: 🤖=私 / 🧑=あなた）

段階ごとにテストして進める。各段階は独立して価値が出る。

### フェーズA：レシート画像を R2 へ（②）
- 🤖 在庫 `storage.py` を `pseudo_freee/storage.py` として独立移植。
- 🤖 `store_voucher_image` / 画像取得 / 削除を storage 経由に差し替え。
- 🤖 テスト追加（env 未設定=ローカル、設定時=R2）。
- 🧑 `pseudo-freee` サービスに R2 の env を設定。
- ✅ 検証: 画像が R2 に保存され、再デプロイ後も表示できる。

### フェーズB：取引・証憑情報を Postgres へ（①）
- 🤖 `pseudo_freee/db.py`（薄いアダプタ）を新設し、SQLite/Postgres を `DATABASE_URL` で切替。
- 🤖 既存の全 SQL 呼び出しをアダプタ経由へ。スキーマDDL・採番・重複回避を方言対応。
- 🤖 テスト更新（在庫 `test_postgres.py` と同じく、テスト用 Neon で検証。本番DBとは必ず分離）。
- 🧑 疑似freee 専用 Neon を作成し `DATABASE_URL` を設定。
- ✅ 検証: 取引・証憑が Postgres に保存され、再デプロイ後も残る。

### フェーズC：本番ライブ検証・仕上げ
- 🤖🧑 レシート登録 → **疑似freee を再デプロイ** → データが残ることを目視。
- 🤖 README / 本書を更新。問題なければ `main` マージ。

---

## 10. テスト計画

- 単体: ローカル（SQLite/ローカル保存）で既存テストが全て緑のまま。
- 結合: テスト用 Neon ＋ テスト用 R2（または擬似）で、保存→再取得→削除の往復。
- 回帰: API/画面の契約（レスポンス形・画面要素）が不変であること。
- 受け入れ（本番）: 「レシート登録 → 再デプロイ → 残っている」を実機で確認。

---

## 11. 受け入れ条件（Definition of Done）

- [ ] 公開疑似freee でレシートを登録 → サービス再デプロイ後も**画像と仕訳が残る**。
- [ ] 在庫からの「freee送信」で届いた取引も再デプロイ後に残る。
- [ ] ローカル（env 未設定）では従来どおり SQLite＋フォルダで動く。
- [ ] 疑似freee のDBが在庫とは分離されている。
- [ ] 秘密情報は env のみ（Git に出ていない）。
- [ ] 既存テスト＋新規テストが全て緑。

---

## 12. リスクと対策

| リスク | 対策 |
|---|---|
| SQLite→Postgres の方言差で不具合 | アダプタに集約し、在庫 `db.py` の実証済みパターンを踏襲。テスト用 Neon で先に検証。 |
| 本番DBを誤って消す | テストは**必ず別DB**（在庫 `test_postgres.py` と同じ分離ルール）。 |
| Neon 無料枠の上限 | 疑似freee はデータ量が小さい（デモ規模）。在庫と別DBで影響を局所化。 |
| 既に揮発したデータ | 復元不可。今回以降は永続化されるため再発しない。ローカルの3件は手元に残存。 |

---

## 13. 将来拡張（参考）

- 本物 freee API 連携（OAuth2.0・事業所ID・各種マッピング）は [`PSEUDO_FREEE_REQUIREMENTS.md`](PSEUDO_FREEE_REQUIREMENTS.md) §6.4 を踏襲。
- 永続化により、本物連携時のデータ移行・突合がやりやすくなる（中間構造が安定するため）。

---

_最終更新: A-8 着手時点。実装の進捗は [`EVOLUTION_PLAN.md`](EVOLUTION_PLAN.md) と作業ブランチに反映する。_
