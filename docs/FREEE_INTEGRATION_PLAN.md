# freee連携・疑似freeeデモ 実装計画

> 📌 **発展方針の正本は [`EVOLUTION_PLAN.md`](EVOLUTION_PLAN.md) です。** 本書は freee 連携の機能詳細です。全体の進め方・採用スタックは正本を優先してください。

## 目的

現在の在庫管理ダッシュボードは、仕入・売上登録時にfreee送信用の中間データを `freee_sync_queue` に保存しています。

現時点では本物のfreee APIには送信していません。次のステップとして、まずはアプリ内に「疑似freee」を作り、freeeに送る想定のデータがどのように会計側へ流れるかをデモできるようにします。

## 方針

本物のfreee API連携に進む前に、以下の順序で実装します。

1. 疑似freee会計台帳を作る
2. `freee_sync_queue` のデータを疑似freeeへ送信する
3. 送信済み・失敗・再送待ちを管理する
4. 送信前JSONと疑似freee登録結果を比較できるようにする
5. その後、本物のfreee API連携へ差し替える

## 現在すでにあるもの

| 要素 | 内容 |
|---|---|
| `freee_sync_queue` | freee送信待ちデータを保存するキュー |
| `build_freee_payload` | 仕入・売上明細からfreee送信用の中間JSONを作る関数 |
| `/api/freee-sync-queue` | 送信待ちキューを取得するAPI |
| `/api/freee-preview` | freee送信用JSONを確認するAPI |
| 送信前レビュー | 画面上で中間JSONを表示する領域 |

## 疑似freeeで追加する機能

| 機能 | 内容 |
|---|---|
| 疑似freee取引台帳 | 送信された仕入・売上データを会計取引として保存 |
| 疑似freee送信ボタン | キューのデータを疑似freeeへ登録 |
| 二重送信防止 | 同じキューIDを複数回送れないようにする |
| 送信ステータス更新 | 成功時は `sent`、失敗時は `failed`、再送時は `retry` |
| 疑似freee一覧画面 | 収入・支出、取引先、請求書番号、金額、税区分、元データを表示 |
| エラー確認 | 送信失敗理由をキューに保存し、画面で確認できるようにする |

## 疑似freeeのデータ項目

疑似freee取引台帳には、最低限以下を保存します。

| カラム | 内容 |
|---|---|
| id | 疑似freee取引ID |
| queue_id | 元のfreee送信キューID |
| source_type | `purchase` または `sale` |
| source_id | 元の仕入・売上明細ID |
| deal_type | `expense` または `income` |
| issue_date | 発生日 |
| due_date | 支払・入金予定日 |
| partner_name | 取引先 |
| invoice_no | 請求書番号・注文番号 |
| account_item_name | 勘定科目 |
| tax_category | 税区分 |
| amount | 金額 |
| payload_json | 送信された元JSON |
| created_at | 登録日時 |

## 画面仕様

既存の `freee送信待ちキュー` を以下のように変更します。

- `確認` ボタン: 送信前JSONを表示
- `疑似freeeへ送信` ボタン: 疑似freee台帳へ登録
- 送信済みの場合はボタンを非表示または無効化
- 失敗時はエラー内容を表示

追加で `疑似freee取引一覧` セクションを作ります。

表示項目:

- 取引ID
- 区分
- 発生日
- 取引先
- 請求書番号
- 勘定科目
- 税区分
- 金額
- 元データ

## 本物のfreee APIへ進む時の追加要件

疑似freeeができた後、本物のfreee API連携では以下を追加します。

- OAuth2.0認証
- アクセストークン・リフレッシュトークン管理
- 事業所IDの選択
- freee側の取引先、勘定科目、税区分、品目、部門マッピング
- API送信エラー処理
- レート制限対応
- 送信済み取引の訂正・取消方針

## 実装ステップ

1. DBに `pseudo_freee_deals` テーブルを追加する
2. `freee_sync_queue` のpayloadを読み取る関数を作る
3. payloadを疑似freee取引へ変換する関数を作る
4. `/api/pseudo-freee/send` を追加する
5. `/api/pseudo-freee/deals` を追加する
6. 送信成功時に `freee_sync_queue.status = 'sent'` に更新する
7. 既存の `送信済みにする` ボタンを `疑似freeeへ送信` に変更する
8. 画面に `疑似freee取引一覧` を追加する
9. 二重送信防止とエラー表示のテストを追加する
10. 将来、本物freee API送信処理へ差し替えられるよう、送信処理を関数として分離する

## テスト観点

- キューのpendingデータを疑似freeeへ送信できること
- 送信後、キューがsentになること
- 同じキューを二重送信できないこと
- 疑似freee取引一覧に送信結果が表示されること
- 送信前JSONと疑似freee取引内容が一致すること
- 送信失敗時にキューへエラーメッセージが残ること
- 在庫元帳・在庫一覧・シミュレーションに影響しないこと

## 判断

今すぐ本物のfreee APIへ進むより、まず疑似freeeを作る方が良いです。

理由:

- freee契約やOAuth設定なしで連携デモができる
- 会計連携のデータフローを説明しやすい
- 送信キュー、二重送信防止、エラー処理を先に検証できる
- 将来の本物freee連携に移行しやすい

