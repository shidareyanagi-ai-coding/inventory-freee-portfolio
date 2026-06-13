# 在庫管理ダッシュボード

freee連携を第2ステップで追加しやすいように、仕入・売上を明細単位で保持する在庫管理ダッシュボードです。

## 起動方法

```powershell
python app.py
```

ブラウザで以下を開きます。

```text
http://127.0.0.1:8000
```

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

現在はSQLiteを使っています。別途DBサーバーを用意しなくても、`python app.py` の起動時に `inventory.db` が自動作成されます。

SQLiteはMVP検証には十分ですが、本番の実務利用ではPostgreSQLへの移行を前提にします。移行の前に、在庫元帳、仕入・売上の流れ、freee連携に必要な項目を固め、その後にDBアクセス層を分離してPostgreSQL接続へ対応します。

## 関連ドキュメント

- [DESIGN_HANDOFF.md](DESIGN_HANDOFF.md): デザインAIへ渡すための画面改善用受け渡し資料
- [FREEE_INTEGRATION_PLAN.md](FREEE_INTEGRATION_PLAN.md): 疑似freeeデモと将来のfreee API連携計画

## freee連携の考え方

第1ステップではfreee API通信は行いません。仕入・売上登録時に `freee_sync_queue` へ送信待ちデータを保存します。

第2ステップでは、OAuth2.0認可、事業所選択、勘定科目・税区分・取引先・品目・部門マッピングを追加し、キューのデータをfreee会計APIへ送信します。

## テスト

```powershell
python -m unittest -v
```
