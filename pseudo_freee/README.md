# Pseudo freee Dashboard

> 📌 **発展方針の正本は [`../docs/EVOLUTION_PLAN.md`](../docs/EVOLUTION_PLAN.md) です。** 全体の進め方・採用スタックは正本を優先してください。

## 概要

このフォルダは、在庫管理ダッシュボードと連携するための「疑似freee会計ダッシュボード」を実装する場所です。

本物のfreee APIへ接続する前に、在庫管理アプリから送信される仕入・売上データを受け取り、会計取引として保存・表示するデモを作ります。

## 想定ローカルURL

```text
http://127.0.0.1:8010
```

## 担当範囲

このフォルダで実装するもの:

- 疑似freee側のWebアプリ
- 疑似freee側DB
- 会計データ受信API
- 手入力の経費登録
- KPIカード、未入金・未払、月次推移
- 取引一覧画面
- 取引詳細画面
- 二重送信防止
- 受信JSONの確認機能

原則として、このフォルダの実装中は `inventory_dashboard` 側の既存コードは変更しません。

## 在庫管理アプリとの連携イメージ

```text
inventory_dashboard
  ↓ freee_sync_queue
  ↓ 疑似freeeへ送信
pseudo_freee
  ↓ 受信API
  ↓ 疑似freee取引台帳
```

## 初期DB方針

最初はSQLiteで実装して構いません。

将来的にはPostgreSQLへの移行、GitHub公開、クラウドデプロイを前提にします。

## 参考資料

上位フォルダの以下を確認してください。

- `ARCHITECTURE.md`
- `DEVELOPMENT_HANDOFF.md`
- `DEPLOYMENT_PLAN.md`
- `docs/PSEUDO_FREEE_PRODUCT_PLAN.md`
- `docs/EXPENSE_CAPTURE_FEATURE_SPEC.md`
- `docs/FREEE_INTEGRATION_PLAN.md`
