# Inventory × Pseudo freee Portfolio

## 概要

このポートフォリオは、在庫管理ダッシュボードと疑似freee会計ダッシュボードを連携させる業務アプリ群です。

目的は、小規模EC・中小企業を想定し、以下の業務フローをデモできるようにすることです。

```text
仕入・売上登録
  ↓
在庫管理
  ↓
在庫元帳・適正在庫シミュレーション
  ↓
freee連携用データ作成
  ↓
疑似freee会計ダッシュボードへ送信
  ↓
会計取引として確認
```

## 想定フォルダ構成

将来的には、以下のような上位フォルダ構成に整理します。

```text
inventory-freee-portfolio/
  README.md
  ARCHITECTURE.md
  DEVELOPMENT_HANDOFF.md
  DEPLOYMENT_PLAN.md
  inventory_dashboard/
    app.py
    inventory.db
    README.md
    ROADMAP.md
    ...
  pseudo_freee/
    app.py
    pseudo_freee.db
    README.md
    ...
  docs/
    DESIGN_HANDOFF.md
    FREEE_INTEGRATION_PLAN.md
    screenshots/
```

## アプリの役割

| フォルダ | 役割 |
|---|---|
| `inventory_dashboard` | 在庫管理、仕入・売上登録、在庫元帳、適正在庫シミュレーション、freee送信キューを担当 |
| `pseudo_freee` | 在庫管理アプリから送信された会計データを受け取り、疑似freee取引台帳として表示 |
| `docs` | 設計資料、デザイン受け渡し資料、スクリーンショット、連携計画を管理 |

## 現在の状態

現在は `inventory_dashboard` 相当のアプリが先に実装されています。

実装済みの主な機能:

- 商品マスタ
- 取引先マスタ
- 仕入明細登録
- 売上明細登録
- 在庫移動台帳
- 商品別在庫元帳
- 取消・訂正履歴
- 今月仕入・売上集計
- 適正在庫シミュレーション
- freee送信待ちキュー
- freee送信前レビューJSON

次の開発対象は `pseudo_freee` です。

## 今後の開発方針

1. `pseudo_freee` フォルダを新規作成する
2. 疑似freee側で会計取引を受け取るAPIを作る
3. 在庫管理アプリの `freee_sync_queue` から疑似freeeへ送信する
4. 疑似freee側で取引一覧・詳細確認画面を作る
5. 将来的に本物のfreee APIへ置き換えられる構成にする
6. GitHub公開・デプロイを見据えてDB設定、環境変数、READMEを整備する

## DB方針

現時点では、両アプリともSQLiteで開始して構いません。

理由:

- ローカルで動作確認しやすい
- ポートフォリオのMVPとして扱いやすい
- DBサーバーなしでデモできる

ただし、本番・デプロイ前提ではPostgreSQLへの移行を想定します。

将来対応:

- DB接続設定を環境変数化する
- SQLite/PostgreSQLを切り替えられる設計にする
- マイグレーション方針を作る
- デプロイ先のDBにPostgreSQLを使う

## 取引先マスタとfreee連携の方針

`inventory_dashboard` では、仕入先・得意先を `partners` マスタとして保持します。

これは単なる入力補助ではなく、将来のfreee連携で重要な中間レイヤーです。

- 仕入・売上入力時は、取引先をクリック/選択して明細へ紐づける
- `purchases` / `sales` には `partner_id` と、登録時点の `partner_name` の両方を保存する
- `partner_name` は過去帳簿の表示を安定させるためのスナップショットとして扱う
- `partners.freee_partner_id` は将来、本物freee側の取引先IDを保存するためのマッピング欄として使う
- 疑似freee連携でも、本物freee連携でも、送信データには `partner_master_id` と `freee_partner_id` を含められる設計にする

この方針により、取引先名の表記揺れを減らし、送信前レビュー、疑似freee、将来の本物freee API連携で同じ取引先を安定して扱えるようにします。

## デプロイ方針

GitHub公開とクラウドデプロイを前提にします。

初期:

- GitHubにコード、README、スクリーンショット、設計資料を公開
- ローカル起動手順を明記
- サンプルデータを自動生成

疑似freeeの具体的な拡張計画は `docs/PSEUDO_FREEE_PRODUCT_PLAN.md` に整理します。

次段階:

- Render、Railway、Fly.ioなどにデモ環境を作る
- `inventory_dashboard` と `pseudo_freee` を別サービスとしてデプロイ
- 疑似freeeのAPI URLを環境変数で指定する

例:

```text
INVENTORY_APP_URL=https://inventory-dashboard.example.com
PSEUDO_FREEE_API_URL=https://pseudo-freee.example.com
```
