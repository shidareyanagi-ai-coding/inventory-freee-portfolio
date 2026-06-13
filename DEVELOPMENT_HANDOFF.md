# Development Handoff Draft

## 新しいチャット/AIへの依頼文

以下を新しいCodexチャットに渡してください。

```text
これから、在庫管理ダッシュボードと接続するための「疑似freee会計ダッシュボード」を作成したいです。

あなたの担当範囲は `pseudo_freee` フォルダです。

既存の `inventory_dashboard` フォルダは参照してよいですが、原則として変更しないでください。

目的は、本物のfreee APIに接続する前に、在庫管理アプリから送信される仕入・売上データが、会計システム側でどのように取引として登録・確認されるかをデモできるようにすることです。

前提:
- 既存の在庫管理ダッシュボードがあります。
- 既存アプリでは、仕入・売上登録時にfreee送信用の中間データを `freee_sync_queue` に保存しています。
- 仕入先・得意先は `partners` マスタで管理されています。
- 仕入・売上明細には `partner_id` と登録時点の `partner_name` が保存されています。`partner_name` は過去帳簿を安定表示するためのスナップショットです。
- `partners.freee_partner_id` は将来、本物freee側の取引先IDを保存するマッピング欄です。
- 疑似freee側で受け取る送信データには、`partner_master_id` と `freee_partner_id` が含まれる可能性があります。
- 将来的には本物のfreee APIへ送信する予定です。
- まずは疑似freeeアプリに送信して、連携デモを作ります。
- 現段階ではSQLiteで構いません。
- ただし、将来的にはPostgreSQLへ移行し、GitHub公開・デプロイする前提の構成にしてください。

希望する構成:
- 上位フォルダの中に `inventory_dashboard` と `pseudo_freee` を並べる構成を想定しています。
- 疑似freee側は独立したローカルWebアプリにしてください。
- ローカルでは `http://127.0.0.1:8010` で起動する想定です。
- 在庫管理アプリ側は `http://127.0.0.1:8000` です。

疑似freeeアプリに欲しい機能:
1. 在庫管理アプリから送信された会計データを受け取るAPI
2. 受信した取引データを疑似freee側DBに保存
3. 収入/支出の取引一覧
4. 取引先、請求書番号、発生日、支払/入金予定日、勘定科目、税区分、金額の表示
5. 元データのsource_type/source_id/queue_idの表示
6. 受信JSONの詳細確認
7. 二重送信防止
8. 送信失敗時のエラー返却
9. 将来本物freee APIへ置き換えやすい構成

在庫管理アプリ側との接続イメージ:

在庫管理アプリ
  ↓
freee_sync_queue
  ↓
疑似freeeへ送信API
  ↓
pseudo_freee側の受信API
  ↓
疑似freee取引台帳に保存
  ↓
疑似freee画面で取引確認

まずは `ARCHITECTURE.md` と `FREEE_INTEGRATION_PLAN.md` を読み、疑似freeeアプリの設計方針と実装ステップを提案してください。
その後、問題なければ `pseudo_freee` フォルダを作成して実装に進んでください。
```

## 担当範囲

新しいAIの担当:

- `pseudo_freee` フォルダ
- 疑似freeeアプリ
- 疑似freee側DB
- 疑似freee側API
- 疑似freee側画面

原則触らない範囲:

- `inventory_dashboard` の既存実装
- 在庫計算ロジック
- 適正在庫シミュレーション
- 在庫元帳

将来的に接続時だけ変更する範囲:

- inventory_dashboard側の `freee_sync_queue`
- 疑似freee送信ボタン
- 疑似freee API URL設定

## 注意点

- 疑似freeeは本物のfreeeではなく、連携デモ用の会計システムです。
- ただし、本物freee APIへ移行しやすいデータ構造にしてください。
- 最初はSQLiteでよいですが、PostgreSQL移行を想定してください。
- API URLやDB接続情報は将来的に環境変数化してください。
- GitHub公開・デプロイ前提でREADMEを整えてください。
