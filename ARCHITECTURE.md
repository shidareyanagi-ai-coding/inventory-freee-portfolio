# Architecture Draft

> 📌 **発展方針の正本は [`docs/EVOLUTION_PLAN.md`](docs/EVOLUTION_PLAN.md) です。** 本書は2アプリ連携の設計メモ（旧MVP）です。全体の進め方・採用スタックは正本を優先してください。

## 全体像

このポートフォリオは、2つの独立した業務アプリを連携させる構成です。

```text
inventory_dashboard
  - 商品管理
  - 取引先管理
  - 仕入・売上登録
  - 在庫元帳
  - 適正在庫シミュレーション
  - freee送信キュー

        ↓ API送信

pseudo_freee
  - 会計取引受信
  - 疑似freee取引台帳
  - 収入・支出一覧
  - 送信元データ確認
```

本物のfreeeは外部サービスであるため、疑似freeeも別アプリとして作る方針です。

## 連携フロー

```text
1. inventory_dashboardで仕入または売上を登録
2. 在庫移動台帳に記録
3. freee_sync_queueに送信用JSONを保存
4. ユーザーが「疑似freeeへ送信」を実行
5. pseudo_freeeの受信APIへJSONをPOST
6. pseudo_freee側で会計取引として保存
7. inventory_dashboard側のキューをsentに更新
8. pseudo_freee画面で取引を確認
```

## inventory_dashboard側

主な責務:

- 商品マスタを管理する
- 取引先マスタを管理する
- 仕入・売上を登録する
- 在庫数を在庫移動台帳から計算する
- 適正在庫をシミュレーションする
- freee送信用の中間JSONを作る
- 送信キューを管理する

取引先マスタの位置づけ:

- 仕入先・得意先は `partners` で管理する
- 明細側の `partner_id` は取引先マスタとの接続に使う
- 明細側の `partner_name` は登録時点の名称スナップショットとして使う
- `partners.freee_partner_id` は将来の本物freee取引先IDマッピングに使う
- freee送信用JSONには `partner_master_id` と `freee_partner_id` を含められるため、疑似freeeにも本物freeeにも同じ思想で接続できる

将来的に追加する責務:

- 疑似freee送信APIクライアント
- 送信成功/失敗のステータス更新
- 再送処理
- 送信済みデータの訂正・取消方針

## pseudo_freee側

主な責務:

- inventory_dashboardから送られたJSONを受信する
- 収入・支出の取引データとして保存する
- 取引一覧を表示する
- 取引詳細で元JSONを確認できるようにする
- 二重送信を防止する

疑似freee側は、本物のfreee APIを模した「受け口」として設計します。

## API設計案

### 疑似freee受信API

```text
POST /api/deals
```

役割:

- freee送信用JSONを受け取る
- 疑似freee取引台帳に保存する
- 成功時に疑似freee取引IDを返す

リクエスト例:

```json
{
  "queue_id": 12,
  "source_type": "purchase",
  "source_id": 4,
  "payload": {
    "api_target": "freee_accounting_deal",
    "issue_date": "2026-06-12",
    "due_date": "2026-07-31",
    "type": "expense",
    "partner_master_id": 1,
    "partner_name": "東京サプライ",
    "freee_partner_id": "",
    "invoice_no": "P-202606-002",
    "details": [
      {
        "sku": "SKU-USB-C-001",
        "description": "USB-Cケーブル 1m",
        "quantity": 25,
        "unit_price": 490,
        "tax_rate": 10,
        "tax_category": "課税仕入 10%",
        "amount": 13475,
        "account_item_name": "仕入高"
      }
    ]
  }
}
```

レスポンス例:

```json
{
  "ok": true,
  "pseudo_freee_deal_id": 101
}
```

### 疑似freee取引一覧API

```text
GET /api/deals
```

役割:

- 疑似freeeに登録された取引一覧を返す

## データ設計案

### pseudo_freee_deals

| カラム | 内容 |
|---|---|
| id | 疑似freee取引ID |
| queue_id | 在庫管理側の送信キューID |
| source_type | `purchase` または `sale` |
| source_id | 元の仕入・売上ID |
| deal_type | `expense` または `income` |
| issue_date | 発生日 |
| due_date | 支払・入金予定日 |
| partner_name | 取引先 |
| partner_master_id | 在庫管理側の取引先マスタID |
| freee_partner_id | 本物freee側の取引先ID。未連携時は空文字 |
| invoice_no | 請求書番号・注文番号 |
| account_item_name | 勘定科目 |
| tax_category | 税区分 |
| amount | 金額 |
| payload_json | 受信したJSON |
| created_at | 登録日時 |

## デプロイ時の構成案

ローカル:

```text
inventory_dashboard: http://127.0.0.1:8000
pseudo_freee: http://127.0.0.1:8010
```

デプロイ:

```text
inventory_dashboard: https://inventory-dashboard.example.com
pseudo_freee: https://pseudo-freee.example.com
```

inventory_dashboard側は、疑似freeeのURLを環境変数で持ちます。

```text
PSEUDO_FREEE_API_URL=http://127.0.0.1:8010
```

## 将来の本物freee連携

疑似freee連携が安定した後、本物のfreee APIへ移行します。

追加が必要なもの:

- OAuth2.0
- アクセストークン更新
- 事業所ID
- 勘定科目・税区分・取引先マッピング
- APIエラー処理
- レート制限対応
- 本物freee側の取引ID保存
