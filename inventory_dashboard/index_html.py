# 在庫管理ダッシュボードのフロント (単一HTMLページ)。
# app.py(FastAPI) から読み込み、GET / で配信する。Plan A では既存HTMLをそのまま使う。
#
# A-3: Clerk のサインインゲートとトークン付与を追加。
#   - render_index() がサーバ側の値（公開キー・設定有無・dev フラグ）を埋め込む。
#   - api() は毎回 getAuthToken() で Bearer を付け、認可は常に FastAPI 側が判定する。
#   - 公開キー(pk_...)はブラウザに出してよい。秘密キー(CLERK_SECRET_KEY)は埋め込まない。

import json

_INDEX_TEMPLATE = r"""
<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>在庫管理ダッシュボード</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --text: #20242a;
      --muted: #68717d;
      --line: #d8dde5;
      --accent: #256c64;
      --accent-2: #b64b35;
      --warn: #a96500;
      --danger: #b3261e;
      --ok: #227447;
    }
    * { box-sizing: border-box; }
    body { margin: 0; font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
    header { padding: 18px 24px; background: #24313d; color: white; display: flex; align-items: center; justify-content: space-between; gap: 16px; }
    h1 { margin: 0; font-size: 20px; letter-spacing: 0; }
    main { padding: 16px 20px 28px; max-width: 1280px; margin: 0 auto; }
    section { margin: 0 0 14px; }
    h2 { margin: 0 0 12px; font-size: 17px; }
    .metrics { display: grid; grid-template-columns: repeat(5, minmax(150px, 1fr)); gap: 12px; }
    .metric, .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
    .metric.risk-alert { background: #fff3f1; border-color: #f3beb7; }
    .metric span { display: block; color: var(--muted); font-size: 12px; }
    .metric strong { display: block; margin-top: 6px; font-size: 22px; }
    .metric.risk-alert span, .metric.risk-alert strong { color: var(--danger); }
    .top-grid { display: grid; grid-template-columns: minmax(430px, 1.45fr) minmax(260px, 1fr) minmax(260px, 1fr); gap: 14px; align-items: start; }
    .ledger-entry-grid { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 16px; align-items: start; }
    .ledger-column { display: grid; gap: 14px; align-content: start; min-width: 0; }
    .bottom-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(360px, .72fr); gap: 16px; align-items: start; }
    .entry-panel { position: sticky; top: 14px; }
    .form-tabs { display: grid; grid-template-columns: 1fr 1fr; gap: 4px; padding: 4px; margin: 8px 0 12px; background: #eef1f4; border: 1px solid var(--line); border-radius: 8px; }
    .form-tab { padding: 9px 10px; border-radius: 6px; background: transparent; color: var(--muted); }
    .form-tab.active { background: white; color: var(--accent); box-shadow: 0 1px 2px rgba(32, 36, 42, .08); }
    .transaction-form { display: none; }
    .transaction-form.active { display: block; }
    .transaction-form h2 { margin-top: 4px; font-size: 15px; }
    .transaction-form button[type="submit"] { width: 100%; margin-top: 12px; }
    .partner-add { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; margin-top: 8px; }
    .partner-add button { padding: 9px 12px; white-space: nowrap; }
    .partner-master-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 16px; }
    label { display: block; font-size: 12px; color: var(--muted); margin: 8px 0 4px; }
    input, select { width: 100%; padding: 9px 10px; border: 1px solid var(--line); border-radius: 6px; background: white; font: inherit; }
    button { border: 0; border-radius: 6px; padding: 10px 12px; background: var(--accent); color: white; font-weight: 700; cursor: pointer; }
    button.link { background: transparent; color: var(--accent); padding: 0; text-align: left; text-decoration: underline; }
    button.secondary { background: #4d5966; }
    button.warning { background: var(--warn); }
    table { width: 100%; border-collapse: collapse; background: white; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
    th, td { padding: 9px 10px; border-bottom: 1px solid var(--line); text-align: left; font-size: 13px; }
    th { background: #eef1f4; color: #38424d; }
    tr:last-child td { border-bottom: 0; }
    /* A-3: サインインゲート（Clerk 設定時のみ表示） */
    #signInGate { position: fixed; inset: 0; background: #24313d; display: none; align-items: center; justify-content: center; z-index: 50; }
    #signInGate.show { display: flex; }
    #signInGate .gate-card { background: white; border-radius: 12px; padding: 28px; min-width: 320px; box-shadow: 0 12px 40px rgba(0,0,0,.3); }
    #signInGate h2 { margin: 0 0 12px; }
    body.gated main { filter: blur(3px); pointer-events: none; user-select: none; }
    .user-area { display: flex; align-items: center; gap: 12px; }
    .user-area .role-badge { font-size: 12px; background: rgba(255,255,255,.18); padding: 3px 8px; border-radius: 999px; }
    .status { display: inline-flex; align-items: center; min-height: 22px; padding: 3px 8px; border-radius: 999px; font-size: 12px; line-height: 1; white-space: nowrap; background: #e8eef1; }
    .status.ok { color: var(--ok); }
    .status.warn { color: var(--warn); }
    .status.danger { color: var(--danger); }
    #products th:nth-child(5), #products td:nth-child(5) { min-width: 108px; }
    .note { margin: -4px 0 10px; color: var(--muted); font-size: 13px; }
    .table-total { display: flex; justify-content: flex-end; gap: 18px; align-items: baseline; padding: 10px 12px; background: white; border: 1px solid var(--line); border-top: 0; border-radius: 0 0 8px 8px; font-size: 13px; }
    .table-total strong { font-size: 16px; }
    .match { color: var(--ok); font-weight: 700; }
    .mismatch { color: var(--danger); font-weight: 700; }
    .error { color: var(--danger); font-size: 12px; font-weight: 700; }
    .summary-stack { display: grid; gap: 14px; }
    .summary-box { background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 14px; }
    .summary-box h3 { margin: 0 0 8px; font-size: 15px; }
    .section-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
    .section-head h2 { margin: 0; }
    .inline-control { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; }
    .inline-control select { width: auto; min-width: 110px; padding: 7px 9px; }
    pre { white-space: pre-wrap; word-break: break-word; background: #1f2933; color: #f7fafc; padding: 12px; border-radius: 8px; max-height: 360px; overflow: auto; }
    .message { min-height: 24px; color: var(--accent-2); font-weight: 700; }
    .forecast-controls { display: flex; gap: 16px; flex-wrap: wrap; margin: 4px 0 10px; }
    .chart-wrap { position: relative; height: 320px; margin: 6px 0 14px; }
    .forecast-grid { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr); gap: 16px; align-items: start; }
    .forecast-grid h3.sub { margin: 0 0 8px; font-size: 14px; color: var(--muted); }
    /* A-5: 仕入・売上請求書のAI取り込み */
    .dropzone { border: 2px dashed var(--line); border-radius: 8px; padding: 12px; margin: 10px 0 12px; background: #f9fbfb; text-align: center; transition: border-color .15s, background .15s; }
    .dropzone.dragover { border-color: var(--accent); background: #eef5f3; }
    .dropzone.busy { opacity: .7; }
    .dz-text { margin: 0 0 8px; font-size: 13px; color: var(--muted); }
    .dropzone input[type="file"] { width: 100%; }
    .ai-pill { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px; background: #eef3f1; color: var(--accent); border: 1px solid #cfe0da; }
    label.low-confidence { color: var(--warn); font-weight: 700; }
    label.low-confidence::after { content: " ⚠ 要確認"; font-size: 11px; font-weight: 700; }
    .field-low { border-color: var(--warn); background: #fff8ec; }
    /* 請求書の小計/消費税/合計を確認するための欄（数量×単価×税率から自動計算） */
    .amount-summary { margin: 12px 0 4px; padding: 10px 12px; background: #f4f7f6; border: 1px solid var(--line); border-radius: 8px; font-size: 13px; }
    .amount-summary > div { display: flex; justify-content: space-between; align-items: baseline; padding: 3px 0; }
    .amount-summary > div.total { margin-top: 4px; padding-top: 7px; border-top: 1px solid var(--line); }
    .amount-summary span { color: var(--muted); }
    .amount-summary strong { font-variant-numeric: tabular-nums; }
    .amount-summary .total strong { font-size: 16px; color: var(--accent); }
    /* 証憑一覧は全幅で使い、各列を潰さない（取引先名は横書きで折り返す）。詳細は一覧の下に表示。 */
    .voucher-grid { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; align-items: start; }
    .voucher-grid h3.sub { margin: 4px 0 8px; font-size: 14px; color: var(--muted); }
    #voucherList th, #voucherList td { vertical-align: top; white-space: normal; word-break: break-word; }
    #voucherList th:nth-child(3), #voucherList td:nth-child(3) { min-width: 8em; } /* 取引先(AI): 横書き2段に収まる幅 */
    .voucher-thumb { max-width: 240px; max-height: 220px; border: 1px solid var(--line); border-radius: 6px; display: block; margin: 6px 0; }
    .badge { display: inline-block; font-size: 11px; padding: 2px 7px; border-radius: 4px; }
    .badge.registered { background: #e6f3ec; color: var(--ok); }
    .badge.draft { background: #fdeee9; color: var(--accent-2); }
    .danger-link { background: none; border: 0; color: var(--danger); font-weight: 700; cursor: pointer; padding: 2px 4px; }
    @media (max-width: 900px) {
      .metrics, .top-grid, .ledger-entry-grid, .bottom-grid, .forecast-grid, .voucher-grid { grid-template-columns: 1fr; }
      .entry-panel { position: static; }
      main { padding: 12px; }
      table { display: block; overflow-x: auto; }
    }
    /* スマホ幅: 2列で詰まりがちな所を1列に積み、入力欄が潰れないようにする */
    @media (max-width: 560px) {
      header { padding: 14px 16px; flex-wrap: wrap; }
      h1 { font-size: 17px; }
      .metrics { gap: 8px; }
      .partner-add { grid-template-columns: 1fr; }
      .partner-add button { width: 100%; }
      .forecast-controls { flex-direction: column; align-items: stretch; }
      .section-head { flex-wrap: wrap; }
      .inline-control { width: 100%; }
      .inline-control select { width: 100%; }
      input, select, button { font-size: 16px; } /* iOSの自動ズーム防止 */
    }
  </style>
  <!-- A-4: 実績線＋予測線＋信頼区間の描画に Chart.js を CDN から読み込む（公開用デモ）。 -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
  <script>
    // サーバが埋め込む認証設定（公開キーは出して良い／秘密キーは出さない）。
    window.__APP_CONFIG__ = __APP_CONFIG_JSON__;
  </script>
</head>
<body>
  <div id="signInGate">
    <div class="gate-card">
      <h2>サインイン</h2>
      <div id="clerk-signin"></div>
    </div>
  </div>
  <header>
    <h1>在庫管理ダッシュボード</h1>
    <div class="user-area">
      <span class="role-badge" id="roleBadge" hidden></span>
      <a href="/launcher" style="color:#cfe8ff;font-size:13px;text-decoration:none;white-space:nowrap;">🏠 アプリ入口</a>
      <button class="secondary" onclick="loadAll()">更新</button>
      <div id="clerk-user"></div>
    </div>
  </header>
  <main>
    <section class="metrics" id="metrics"></section>
    <div id="modelWarning" style="display:none;margin:0 0 14px;padding:10px 14px;border:1px solid #f3beb7;background:#fff7f6;color:var(--danger);border-radius:8px;font-size:13px;font-weight:600;"></div>
    <section class="top-grid">
      <div class="panel">
        <h2>在庫一覧</h2>
        <div id="products"></div>
      </div>
      <div class="summary-box">
        <h3>今月仕入 商品別（税込）</h3>
        <div id="monthlyPurchases"></div>
      </div>
      <div class="summary-box">
        <h3>今月売上 商品別（税込）</h3>
        <div id="monthlySales"></div>
      </div>
    </section>
    <section class="ledger-entry-grid">
      <div class="ledger-column">
      <div class="panel">
        <div class="section-head">
          <h2 id="ledgerTitle">在庫元帳</h2>
          <label class="inline-control">表示商品
            <select id="ledgerProductSelect" onchange="loadLedger(this.value)"></select>
          </label>
        </div>
        <p class="note" id="ledgerNote">在庫一覧の商品名をクリックすると、仕入・売上・初期在庫から現在庫に至る記録を表示します。</p>
        <div id="ledger"></div>
      </div>
      <div class="panel" id="voucherSection">
        <div class="section-head">
          <h2>証憑（仕入・売上の請求書）</h2>
        </div>
        <p class="note">上の「登録」パネルで請求書を取り込むと、ここに<strong>元画像・AIの読み取り結果・取込先（仕入／売上）</strong>が残ります。後から見比べ・削除ができます。<br>多いときは折りたたんで表示します。</p>
        <div class="voucher-grid">
          <div id="voucherList"></div>
          <div id="voucherDetail"></div>
        </div>
      </div>
      </div>
      <aside class="panel entry-panel">
        <h2>登録</h2>
        <div class="message" id="message"></div>
        <div class="form-tabs" role="tablist" aria-label="登録種別">
          <button class="form-tab active" type="button" data-form="purchaseForm" onclick="showTransactionForm('purchaseForm')">仕入</button>
          <button class="form-tab" type="button" data-form="saleForm" onclick="showTransactionForm('saleForm')">売上</button>
        </div>
        <!-- A-5: 仕入・売上の請求書からAI取り込み（下書きを下のフォームに反映。登録は人が押す）。 -->
        <div id="invoiceDrop" class="dropzone" tabindex="0">
          <p class="dz-text">📄 <strong>請求書</strong>をドラッグ&ドロップ / 貼り付け（Ctrl+V）<br>または下から選択・カメラ撮影</p>
          <input type="file" id="invoiceImage" accept="image/*" capture="environment">
          <p class="note" id="invoiceStatus">AIが「<span id="dzKindLabel">仕入</span>」フォームに下書きを反映します。⚠の項目は確認のうえ登録してください。</p>
        </div>
        <!-- A-6 BYO-key: 自分のAnthropicキーを入れると本物のAIで解析（既定はサンプル動作）。 -->
        <div id="aiKeyPanel" style="margin:8px 0;padding:8px 10px;border:1px solid var(--border,#e2e2e2);border-radius:8px;background:rgba(0,0,0,0.02);">
          <p class="note" id="aiKeyStatus" style="margin:0 0 6px;"></p>
          <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center;">
            <input type="password" id="anthropicKeyInput" placeholder="sk-ant-... を貼り付け" autocomplete="off" spellcheck="false" style="flex:1;min-width:150px;">
            <button type="button" id="aiKeySave">有効化</button>
            <button type="button" id="aiKeyClear">解除</button>
          </div>
          <p class="note" style="margin:6px 0 0;font-size:0.82em;line-height:1.45;">🔒 キーは<strong>このブラウザにだけ</strong>保存され、解析の時だけサーバへ送られます（<strong>サーバには保存しません</strong>）。料金はあなたのAnthropicアカウントに発生します。未入力ならサンプル動作（無料）。</p>
        </div>
        <div>
          <form id="purchaseForm" class="transaction-form active">
            <input type="hidden" name="voucher_id">
            <h2>仕入明細</h2>
            <label>商品</label><select name="product_id"></select>
            <label>仕入先</label><select name="partner_name" data-partner-type="supplier" required></select>
            <div class="partner-add">
              <input id="newSupplierName" placeholder="新しい仕入先名">
              <button type="button" onclick="addPartner('supplier', 'newSupplierName', 'purchaseForm')">追加</button>
            </div>
            <label>請求書番号</label><input name="invoice_no" required>
            <label>仕入日（入庫日）</label><input type="date" name="transaction_date" required>
            <label>数量</label><input type="number" name="quantity" min="1" required>
            <label>単価</label><input type="number" name="unit_price" min="0" required>
            <label>税率</label><input type="number" name="tax_rate" value="10">
            <label>税区分</label><input name="tax_category" value="課税仕入 10%">
            <div class="amount-summary">
              <div><span>小計（税抜）</span><strong data-summary="subtotal">¥0</strong></div>
              <div><span>消費税</span><strong data-summary="tax">¥0</strong></div>
              <div class="total"><span>合計（税込）</span><strong data-summary="total">¥0</strong></div>
            </div>
            <label>支払予定日</label><input type="date" name="due_date">
            <button type="submit">仕入登録</button>
          </form>
          <form id="saleForm" class="transaction-form">
            <input type="hidden" name="voucher_id">
            <h2>売上明細</h2>
            <label>商品</label><select name="product_id"></select>
            <label>得意先</label><select name="partner_name" data-partner-type="customer" required></select>
            <div class="partner-add">
              <input id="newCustomerName" placeholder="新しい得意先名">
              <button type="button" onclick="addPartner('customer', 'newCustomerName', 'saleForm')">追加</button>
            </div>
            <label>請求書/注文番号</label><input name="invoice_no" required>
            <label>売上日</label><input type="date" name="transaction_date" required>
            <label>数量</label><input type="number" name="quantity" min="1" required>
            <label>単価</label><input type="number" name="unit_price" min="0" required>
            <label>税率</label><input type="number" name="tax_rate" value="10">
            <label>税区分</label><input name="tax_category" value="課税売上 10%">
            <div class="amount-summary">
              <div><span>小計（税抜）</span><strong data-summary="subtotal">¥0</strong></div>
              <div><span>消費税</span><strong data-summary="tax">¥0</strong></div>
              <div class="total"><span>合計（税込）</span><strong data-summary="total">¥0</strong></div>
            </div>
            <label>入金予定日</label><input type="date" name="due_date">
            <button type="submit">売上登録</button>
          </form>
        </div>
      </aside>
    </section>
    <section class="panel" id="partnerMasterSection">
      <h2>取引先マスタ</h2>
      <p class="note">登録済みの仕入先・得意先を修正・削除できます。名前を直すと、その取引先の過去の仕入/売上の表示名も一緒に更新され、疑似freeeへ送信済みの取引の取引先名も更新します（共有ID連携）。取引のある取引先は削除できません（名前を直す場合は「編集」を使ってください）。</p>
      <div class="partner-master-grid">
        <div><h3 class="sub">仕入先</h3><div id="supplierMaster"></div></div>
        <div><h3 class="sub">得意先</h3><div id="customerMaster"></div></div>
      </div>
    </section>
    <section class="panel">
      <div class="section-head">
        <h2>適正在庫シミュレーション</h2>
      </div>
      <p class="note" id="forecastNote">過去売上から月末販売数、リードタイム需要、必要在庫、推奨発注量を計算します。</p>
      <div id="forecastSimulation"></div>
    </section>
    <section class="panel" id="forecastMlSection">
      <div class="section-head">
        <h2>需要予測レベル2（実績×予測）</h2>
        <button type="button" id="runForecastBtn" onclick="runForecastBatch()">予測バッチを実行</button>
      </div>
      <p class="note" id="forecastMlNote">baseline / SARIMA / LightGBM の精度(MAE)を比較し、実績線＋予測線＋信頼区間(80%)を表示します。</p>
      <div class="note" style="background:#f4f7fb;border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin:0 0 12px;">
        <strong>「予測バッチを実行」とは？</strong>＝機械学習の再計算ボタンです（重い処理なので画面表示とは分けています）。
        <br>・<b>いつ押す</b>: 売上を登録した／需要履歴CSVを取り込んだ／クリーンスタートした後など、<b>データが変わったとき</b>。押すまでは前回の結果のまま（未計算の商品は簡易計算）。
        <br>・<b>何をする</b>: 現在の需要データで3モデルを学習し、①未来の需要予測（チャートの予測線）②バックテスト（直近28日で精度MAEを測り<b>最良モデル★を決定</b>）③各商品の必要在庫・推奨発注量、をまとめて更新します。
      </div>
      <div class="forecast-controls">
        <label class="inline-control">商品
          <select id="forecastProduct" onchange="onForecastProductChange()"></select>
        </label>
        <label class="inline-control">モデル
          <select id="forecastModel" onchange="onForecastModelChange()"></select>
        </label>
      </div>
      <div class="chart-wrap"><canvas id="forecastChart"></canvas></div>
      <div>
        <h3 class="sub">モデル比較と発注判定 <span id="forecastTargetName" style="font-weight:normal;color:var(--muted);font-size:13px;"></span></h3>
        <div id="forecastCandidates"></div>
        <p class="note">MAE＝直近28日ホールドアウトの平均予測誤差（個/日・全商品平均・小さいほど正確）。★＝MAEが最小のモデル（在庫は個数で発注するため MAE で選定し、推奨発注量に採用）。「現在在庫」以降は選択商品の値。</p>
      </div>
    </section>
    <section class="bottom-grid">
      <div class="summary-box">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;margin-bottom:8px;">
          <h2 style="margin:0;">freee送信待ちキュー <span id="queueUnsentBadge" class="status danger" style="display:none;"></span></h2>
          <button type="button" id="sendAllBtn" class="warning" onclick="sendAllToPseudoFreee()" style="font-size:13px;padding:5px 12px;">未送信を一括送信</button>
        </div>
        <p style="color:var(--muted);font-size:12px;margin:0 0 8px;">登録は自動でキューに積まれます。<b>未送信を一括送信</b>でまとめて疑似freeeへ。失敗は再度押せばリトライします。</p>
        <div id="queue"></div>
      </div>
      <div class="summary-box">
        <h2>送信前レビュー</h2>
        <pre id="preview">キューの「確認」を押すと、freee送信用の中間データを表示します。</pre>
      </div>
    </section>
    <section class="panel" id="realDataSection">
      <h2>🗂 実データ運用（CSV取込・クリーンスタート）</h2>
      <p style="color:var(--muted);font-size:13px;margin:0 0 12px;">デモではなく、実際の過去データで使うための機能です。</p>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:16px;">
        <div style="border:1px solid var(--line);border-radius:8px;padding:14px;">
          <h3 style="margin:0 0 6px;font-size:14px;">① 需要履歴を CSV で一括取込（予測用）</h3>
          <p style="color:var(--muted);font-size:12px;margin:0 0 10px;">列: <code>date,sku,product_name,quantity,unit_price</code>（1行目に列名）。<b>予測専用の需要履歴</b>として取り込みます（在庫元帳・会計には計上しません＝二重計上なし）。取込後、下の「需要予測レベル2」で <b>予測バッチを実行</b>。再取込は置き換えになります。</p>
          <input type="file" id="salesCsvFile" accept=".csv,text/csv" style="display:block;margin-bottom:8px;">
          <button type="button" id="salesCsvImportBtn" class="secondary">CSV を取り込む</button>
          <p id="salesCsvResult" style="color:var(--muted);font-size:12px;margin:8px 0 0;"></p>
        </div>
        <div style="border:1px solid #f3beb7;border-radius:8px;padding:14px;background:#fff7f6;">
          <h3 style="margin:0 0 6px;font-size:14px;color:var(--danger);">② デモデータを全消去（クリーンスタート）</h3>
          <p style="color:var(--muted);font-size:12px;margin:0 0 10px;">この組織の商品・取引・履歴・予測・証憑を <b>すべて消去</b> します（アカウントとログインは残ります）。実データだけで始めたいときに。</p>
          <button type="button" id="clearDataBtn" style="background:var(--danger);color:#fff;border:none;">デモデータを全消去</button>
        </div>
      </div>
    </section>
    <section class="panel" id="closingInventorySection">
      <h2>📦 決算: 期末在庫を freee へ送る</h2>
      <p style="color:var(--muted);font-size:13px;margin:0 0 12px;">期末時点の在庫評価額を計算し、疑似freee の決算（期末商品・売上原価・BS の「商品」）へ送ります。実地棚卸で帳簿と差（棚卸減耗）があれば、その差額は会計側で<strong>「棚卸減耗損」</strong>として計上され、売上原価に算入されます。基準日を空にすると現在時点で計算します。</p>
      <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;">
        <label style="font-size:13px;">対象期(YYYYMM)<br><input type="text" id="closingPeriod" placeholder="202603" inputmode="numeric" style="margin-top:4px;"></label>
        <label style="font-size:13px;">基準日(任意)<br><input type="date" id="closingAsOf" style="margin-top:4px;"></label>
        <button type="button" id="closingCalcBtn" class="secondary">帳簿・実地・減耗を計算</button>
        <button type="button" id="closingPushBtn" class="warning">freeeへ送信</button>
      </div>
      <p id="closingResult" style="color:var(--muted);font-size:12px;margin:10px 0 0;"></p>
      <div class="section-head" style="margin-top:14px;"><h3 class="sub">freee 送信履歴（何を送ったかの記録）</h3></div>
      <p style="color:var(--muted);font-size:12px;margin:0 0 8px;">「freeeへ送信」した期末棚卸の記録です。在庫一覧の帳簿・実地・棚卸減耗損と照らし合わせる確認表になります。</p>
      <div id="closingSends"></div>
      <div class="section-head" style="margin-top:16px;"><h3 class="sub">棚卸減耗を記録（実地棚卸）</h3></div>
      <p style="color:var(--muted);font-size:12px;margin:0 0 8px;">実地カウントが帳簿在庫より少ないときに、<strong>商品ごとに実地数量を入力</strong>して在庫を評価減します（在庫一覧・期末在庫・突合にすぐ反映）。記録後、上の「freeeへ送信」で会計へ棚卸減耗損として連携します。</p>
      <div style="display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end;">
        <label style="font-size:13px;">商品<br><select id="shrinkProduct" style="margin-top:4px;"></select></label>
        <label style="font-size:13px;">実地数量<br><input type="number" id="shrinkPhysicalQty" min="0" style="margin-top:4px;"></label>
        <button type="button" id="shrinkBtn" class="secondary">評価減を記録</button>
      </div>
      <p id="shrinkResult" style="color:var(--muted);font-size:12px;margin:10px 0 0;"></p>
    </section>
    <section class="panel" id="reconciliationSection">
      <h2>🔗 会計突合（在庫 ⇄ 疑似freee） <span id="reconBadge" class="status" style="display:none;"></span></h2>
      <p style="color:var(--muted);font-size:13px;margin:0 0 12px;">在庫の「会計に映すべき総額」と疑似freee の記帳額を突き合わせます。差分があれば未送信などのズレです（「未送信を一括送信」「期末在庫を送る」で解消）。</p>
      <button type="button" id="reconBtn" class="secondary">在庫と疑似freee を突合する</button>
      <div id="reconResult" style="margin-top:12px;"></div>
    </section>
  </main>
  <script>
    const yen = new Intl.NumberFormat("ja-JP", { style: "currency", currency: "JPY", maximumFractionDigits: 0 });
    const today = new Date().toISOString().slice(0, 10);
    let currentLedgerProductId = null;
    let currentLedgerData = null;
    let ledgerExpanded = false;
    let currentPartners = { suppliers: [], customers: [] };
    let currentQueueRows = [];
    let queueExpanded = false;
    const QUEUE_COLLAPSE_LIMIT = 8;  // freee送信待ちキューが長いと縦長になるので既定で折りたたむ。
    let currentPreviewKey = null;
    const defaultPreviewText = "キューの「確認」を押すと、freee送信用の中間データを表示します。";
    for (const input of document.querySelectorAll('input[type="date"][required]')) input.value = today;

    // A-3: Clerk セッションがあれば Bearer トークンを取得する（dev モードでは null）。
    async function getAuthToken() {
      try {
        if (window.Clerk && window.Clerk.session) {
          return await window.Clerk.session.getToken();
        }
      } catch (e) { /* 取得失敗時はトークン無しで投げ、サーバが 401 を返す */ }
      return null;
    }

    async function api(path, options = {}) {
      const token = await getAuthToken();
      const headers = Object.assign({}, options.headers);
      if (token) headers["Authorization"] = "Bearer " + token;
      const res = await fetch(path, Object.assign({}, options, { headers }));
      if (res.status === 401) throw new Error("認証が必要です。サインインしてください。");
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "request failed");
      return data;
    }

    async function loadAll() {
      const data = await api("/api/dashboard?model_name=" + encodeURIComponent(currentModel()));
      renderMetrics(data);
      renderProducts(data.products);
      updateModelWarning(currentModel(), data.best_model);
      renderMonthlySummary("monthlyPurchases", data.monthly_purchases, data.monthly_purchase_total, "今月仕入 合計（税込）");
      renderMonthlySummary("monthlySales", data.monthly_sales, data.monthly_sales_total, "今月売上 合計（税込）");
      await loadForecast();
      renderSelects(data.products);
      await loadForecastML(data.products);
      await loadPartners();
      // 選択中の元帳商品が今も存在するかを、いま取得した一覧で確認する。
      // クリーンスタート・商品削除・CSV再取込（id 再採番）で旧 id が消えていると、
      // /api/products/{旧id}/ledger が 404「product not found」になり loadAll 全体が失敗するため。
      const ledgerExists = currentLedgerProductId != null
        && data.products.some(p => String(p.id) === String(currentLedgerProductId));
      if (ledgerExists) {
        await loadLedger(currentLedgerProductId);
      } else if (data.products.length) {
        await loadLedger(data.products[0].id);  // 旧選択は破棄し、先頭商品の元帳を表示
      } else {
        // 商品ゼロ（クリーンスタート直後など）：元帳表示を初期化する
        currentLedgerProductId = null;
        currentLedgerData = null;
        document.getElementById("ledger").innerHTML = "";
        document.getElementById("ledgerTitle").textContent = "在庫元帳";
      }
      const queue = await api("/api/freee-sync-queue");
      renderQueue(queue);
      await loadVouchers();
      await loadClosingSends();
    }

    function renderMetrics(data) {
      window.dashboardStockTotal = Number(data.total_stock_value || 0);
      document.getElementById("metrics").innerHTML = [
        ["在庫総額", yen.format(data.total_stock_value), ""],
        ["商品数", data.product_count, ""],
        ["発注/欠品リスク", data.reorder_count, Number(data.reorder_count || 0) > 0 ? "risk-alert" : ""],
        ["今月仕入（税込）", yen.format(data.monthly_purchase_total), ""],
        ["今月売上（税込）", yen.format(data.monthly_sales_total), ""]
      ].map(([label, value, cls]) => `<div class="metric ${cls}"><span>${label}</span><strong>${value}</strong></div>`).join("");
    }

    function renderProducts(products) {
      const physTotal = products.reduce((sum, p) => sum + Number(p.stock_value || 0), 0);   // 実地
      const bookTotal = products.reduce((sum, p) => sum + Number(p.book_value || 0), 0);    // 帳簿
      const lossTotal = products.reduce((sum, p) => sum + Number(p.shrinkage_value || 0), 0); // 棚卸減耗損
      const dashboardTotal = Number(window.dashboardStockTotal || 0);
      const diff = physTotal - dashboardTotal;
      const diffText = diff === 0 ? `<span class="match">在庫総額（実地）と一致</span>` : `<span class="mismatch">差額 ${yen.format(diff)}</span>`;
      const lossQ = (p) => Number(p.shrinkage_quantity || 0) > 0 ? `<span class="mismatch">-${p.shrinkage_quantity}</span>` : "0";
      const lossV = (p) => Number(p.shrinkage_value || 0) > 0 ? `<span class="mismatch">${yen.format(p.shrinkage_value)}</span>` : yen.format(0);
      document.getElementById("products").innerHTML = table(
        ["SKU", "商品", "必要水準", "帳簿在庫", "実地在庫", "減耗数量", "状態", "帳簿在庫金額", "実地棚卸金額", "棚卸減耗損", "推奨発注量"],
        products.map(p => [
          p.sku,
          `<button class="link" onclick="loadLedger(${p.id})">${p.product_name}</button>`,
          p.required_stock_level,
          p.book_quantity,
          p.stock_quantity,
          lossQ(p),
          status(p.status),
          yen.format(p.book_value),
          yen.format(p.stock_value),
          lossV(p),
          p.recommended_order_quantity,
        ]))
        + `<div class="table-total"><span>合計</span><strong>帳簿 ${yen.format(bookTotal)} ／ 実地 ${yen.format(physTotal)} ／ 棚卸減耗損 ${yen.format(lossTotal)}</strong><span>${diffText}</span></div>`
        + `<p class="note">「帳簿在庫」＝仕入・売上から計算される理論在庫、「実地在庫」＝棚卸でカウントした実在庫、「減耗」＝その差（棚卸減耗損）。実地入力の後も差の経緯が残り、計上根拠を追えます。必要水準・推奨発注量は適正在庫シミュレーション（AI予測）と同じ基準。</p>`;
    }

    function renderMonthlySummary(elementId, rows, total, totalLabel) {
      document.getElementById(elementId).innerHTML = table(["SKU", "商品", "数量", "金額"],
        rows.map(row => [row.sku, row.product_name, row.quantity, yen.format(row.amount)]))
        + `<div class="table-total"><span>${totalLabel}</span><strong>${yen.format(total || 0)}</strong></div>`;
    }

    async function loadForecast() {
      // 予測期間ドロップダウンは廃止（AI予測ベースに一本化したため表示に影響しなかった）。horizon は既定30日。
      const data = await api(`/api/forecast-simulation?model_name=${encodeURIComponent(currentModel())}`);
      renderForecast(data);
    }

    function renderForecast(data) {
      const appliedNote = data.applied_model ? `選択モデル「${modelLabel(data.applied_model)}」` : "AIモデル（最良）";
      document.getElementById("forecastNote").textContent =
        `${appliedNote}の予測で ${data.month_end} までの需要を見込み、各商品の「必要在庫」と「今すぐ発注量」を出しています（「採用モデル」列で商品ごとの使用モデルを確認できます）。必要在庫 = リードタイム需要(入荷までに売れる予測数) + 安全在庫。今すぐ発注量 = max(必要在庫 − 現在在庫, 0)。`;
      document.getElementById("forecastSimulation").innerHTML = table(
        ["SKU", "商品", "現在在庫", "採用モデル", "リードタイム日数", "リードタイム需要", "安全在庫", "必要在庫", "今すぐ発注量", "リードタイム判定", "月末予測販売数", "月末在庫見込み", "月末不足数", "月末判定"],
        data.rows.map(row => [
          row.sku,
          row.product_name,
          row.stock_quantity,
          modelLabel(row.model),
          row.lead_time_days,
          row.lead_time_demand,
          row.safety_stock,
          row.required_inventory,
          row.recommended_order_quantity,
          forecastJudgement(row.lead_time_judgement),
          row.month_end_forecast,
          row.projected_month_end_stock_after_order,
          row.month_end_shortage,
          forecastJudgement(row.month_end_judgement)
        ])
      );
    }

    function forecastJudgement(text) {
      const cls = (text === "発注不要" || text === "月末OK") ? "ok" : (text === "データ不足" ? "warn" : "danger");
      return `<span class="status ${cls}">${text}</span>`;
    }

    // --- 需要予測レベル2（A-4: 実績線＋予測線＋信頼区間） ---------------------
    let forecastChartInstance = null;
    const MODEL_LABELS = { baseline: "ベースライン", sarima: "SARIMA", lightgbm: "LightGBM" };
    function modelLabel(name) { return MODEL_LABELS[name] || name || "—"; }

    // 需要予測レベル2のモデル選択（""＝自動＝最良）。在庫一覧・適正在庫シミュレーション・発注判定を駆動する。
    function currentModel() {
      const el = document.getElementById("forecastModel");
      return el ? (el.value || "") : "";
    }

    // モデルを切り替えたとき：チャート＋在庫一覧／シミュレーション／発注判定／警告を選択モデルで再計算。
    async function onForecastModelChange() {
      await loadForecastChart();
      await refreshModelDrivenViews();
    }

    async function refreshModelDrivenViews() {
      const m = currentModel();
      const data = await api("/api/dashboard?model_name=" + encodeURIComponent(m));
      renderMetrics(data);
      renderProducts(data.products);
      updateModelWarning(m, data.best_model);
      await loadForecast();
      await loadForecastCandidates();
    }

    // 最良でないモデルを選んでいるときは赤い警告を出す（自動＝最良に戻すと消える）。
    function updateModelWarning(selected, bestModel) {
      const el = document.getElementById("modelWarning");
      if (!el) return;
      if (selected && bestModel && selected !== bestModel) {
        el.textContent = `⚠ 現在の予測は最良モデル（${modelLabel(bestModel)}）ではありません。選択中: ${modelLabel(selected)}。在庫一覧・適正在庫シミュレーションはこのモデルで計算中です。最良に戻すには、下の「需要予測レベル2」の『モデル』を『自動（最良）』に選び直してください。`;
        el.style.display = "";
      } else {
        el.style.display = "none";
      }
    }

    async function loadForecastML(products) {
      const select = document.getElementById("forecastProduct");
      const previous = select.value;
      select.innerHTML = products.map(p => `<option value="${p.id}">${p.sku} ${p.product_name}</option>`).join("");
      if (previous && [...select.options].some(o => o.value === previous)) select.value = previous;
      await refreshForecastModelOptions();
      await Promise.all([loadForecastChart(), loadForecastCandidates()]);
    }

    async function refreshForecastModelOptions() {
      try {
        const data = await api("/api/forecast/models");
        const select = document.getElementById("forecastModel");
        const previous = select.value;
        select.innerHTML = '<option value="">自動(最良)</option>'
          + data.models.map(m => `<option value="${m.name}">${m.label}</option>`).join("");
        if (previous && [...select.options].some(o => o.value === previous)) select.value = previous;
      } catch (e) { /* モデル一覧の取得失敗は致命でない */ }
    }

    async function loadForecastChart() {
      const productId = document.getElementById("forecastProduct").value;
      if (!productId) return;
      const model = document.getElementById("forecastModel").value;
      const query = `/api/forecast/series?product_id=${productId}` + (model ? `&model_name=${encodeURIComponent(model)}` : "");
      const data = await api(query);
      renderForecastChart(data);
    }

    function renderForecastChart(data) {
      const actual = data.actual || [];
      const forecast = data.forecast || [];
      const note = document.getElementById("forecastMlNote");
      if (typeof Chart === "undefined") {
        note.textContent = "グラフ描画ライブラリ(Chart.js)を読み込めませんでした（ネットワークをご確認ください）。";
        return;
      }
      if (!forecast.length) {
        note.textContent = "この商品の予測はまだありません。「予測バッチを実行」を押してください。";
      }
      const dates = Array.from(new Set([...actual.map(a => a.date), ...forecast.map(f => f.date)])).sort();
      const actualMap = Object.fromEntries(actual.map(a => [a.date, a.qty]));
      const predMap = Object.fromEntries(forecast.map(f => [f.date, f.predicted]));
      const lowerMap = Object.fromEntries(forecast.map(f => [f.date, f.lower]));
      const upperMap = Object.fromEntries(forecast.map(f => [f.date, f.upper]));
      // 実績の最終日に予測値も置いて、実績線と予測線をつなげる。
      if (actual.length && forecast.length) {
        const lastActual = actual[actual.length - 1].date;
        if (!(lastActual in predMap)) predMap[lastActual] = actualMap[lastActual];
      }
      const pick = (map) => dates.map(d => (d in map ? map[d] : null));
      const band = "rgba(182, 75, 53, 0.12)";
      const datasets = [
        { label: "実績", data: pick(actualMap), borderColor: "#256c64", backgroundColor: "#256c64", borderWidth: 2, pointRadius: 0, tension: 0.2 },
        { label: "予測", data: pick(predMap), borderColor: "#b64b35", borderDash: [6, 4], borderWidth: 2, pointRadius: 0, tension: 0.2, spanGaps: true },
        { label: "信頼区間(80%)", data: pick(upperMap), borderColor: "transparent", backgroundColor: band, pointRadius: 0, fill: "+1", spanGaps: true },
        { label: "下限", data: pick(lowerMap), borderColor: "transparent", backgroundColor: band, pointRadius: 0, fill: false, spanGaps: true },
      ];
      if (forecastChartInstance) forecastChartInstance.destroy();
      forecastChartInstance = new Chart(document.getElementById("forecastChart"), {
        type: "line",
        data: { labels: dates, datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          plugins: { legend: { labels: { filter: (item) => item.text !== "下限" } } },
          scales: {
            x: { ticks: { maxTicksLimit: 10, autoSkip: true } },
            y: { beginAtZero: true, title: { display: true, text: "販売数量 / 日" } },
          },
        },
      });
    }

    // 上の商品ドロップダウンで選んだ商品の「モデル比較＋発注判定」を1つの表で表示する。
    function onForecastProductChange() {
      loadForecastChart();
      loadForecastCandidates();
    }
    async function loadForecastCandidates() {
      const productId = document.getElementById("forecastProduct").value;
      const target = document.getElementById("forecastCandidates");
      const nameEl = document.getElementById("forecastTargetName");
      if (!productId) { target.innerHTML = ""; if (nameEl) nameEl.textContent = ""; return; }
      // 行=3モデル(★最良・MAE昇順)、列=MAE/MAPE(精度)＋現在在庫/必要在庫/今すぐ発注量/判定(選択商品)。商品名は見出しへ。
      const data = await api(`/api/forecast/judgement-by-model?product_id=${productId}`);
      if (nameEl) nameEl.textContent = data.product ? `／ 対象商品: ${data.product.sku} ${data.product.product_name}` : "";
      if (!data.models || !data.models.length) {
        target.innerHTML = '<p class="note">この商品の予測がまだありません。「予測バッチを実行」を押してください。</p>';
        return;
      }
      // 最良(★)行は緑で強調。最良以外を選択中ならその行を黄色＋「選択中」で示す（在庫一覧・シミュもこのモデル）。
      const sel = currentModel();
      const headers = ["モデル", "MAE", "現在在庫", "必要在庫", "今すぐ発注量", "判定"];
      const body = data.models.map(m => {
        const isSel = sel && m.model_name === sel;
        const bg = m.is_best ? ' style="background:#eafaf1;"' : (isSel ? ' style="background:#fff8e1;"' : '');
        const tag = m.is_best ? " ★" : (isSel ? "（選択中）" : "");
        return `<tr${bg}><td>${modelLabel(m.model_name)}${tag}</td>`
          + `<td>${m.mae != null ? Number(m.mae).toFixed(2) : "—"}</td>`
          + `<td>${data.stock_quantity}</td><td>${m.required_inventory}</td>`
          + `<td>${m.recommended_order_quantity}</td><td>${forecastJudgement(m.judgement)}</td></tr>`;
      }).join("");
      target.innerHTML = `<table><thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table>`;
    }

    async function runForecastBatch() {
      const button = document.getElementById("runForecastBtn");
      const original = button.textContent;
      button.disabled = true;
      button.textContent = "計算中…";
      try {
        const result = await api("/api/forecast/run", { method: "POST" });
        document.getElementById("forecastMlNote").textContent =
          `予測を更新しました（最良モデル: ${modelLabel(result.best_model)} ／ 対象 ${result.products_forecasted} 商品 ／ 発注候補 ${result.order_candidates} 件）。`;
        await refreshForecastModelOptions();
        await Promise.all([loadForecastChart(), loadForecastCandidates()]);
      } catch (e) {
        alert(e.message);
      } finally {
        button.disabled = false;
        button.textContent = original;
      }
    }

    function renderSelects(products) {
      const html = products.map(p => `<option value="${p.id}">${p.sku} ${p.product_name}</option>`).join("");
      document.querySelectorAll("select[name='product_id']").forEach(select => {
        const currentValue = select.value;
        select.innerHTML = html;
        if (currentValue && [...select.options].some(option => option.value === currentValue)) {
          select.value = currentValue;
        }
        select.onchange = () => loadLedger(select.value);
      });
      const ledgerSelect = document.getElementById("ledgerProductSelect");
      if (ledgerSelect) {
        const currentValue = String(currentLedgerProductId || ledgerSelect.value || "");
        ledgerSelect.innerHTML = html;
        if (currentValue && [...ledgerSelect.options].some(option => option.value === currentValue)) {
          ledgerSelect.value = currentValue;
        }
      }
      const shrinkSelect = document.getElementById("shrinkProduct");
      if (shrinkSelect) {
        const currentValue = shrinkSelect.value;
        shrinkSelect.innerHTML = html;
        if (currentValue && [...shrinkSelect.options].some(option => option.value === currentValue)) {
          shrinkSelect.value = currentValue;
        }
      }
    }

    async function loadPartners() {
      currentPartners = await api("/api/business-partners");
      renderPartnerSelects();
      renderPartnerMaster();
    }

    function partnerRowsFor(type) {
      return (type === "supplier" ? currentPartners.suppliers : currentPartners.customers) || [];
    }

    function renderPartnerMaster() {
      const configs = [
        { type: "supplier", elId: "supplierMaster" },
        { type: "customer", elId: "customerMaster" }
      ];
      for (const c of configs) {
        const el = document.getElementById(c.elId);
        if (!el) continue;
        const rows = partnerRowsFor(c.type);
        if (!rows.length) { el.innerHTML = `<p class="note">登録なし</p>`; continue; }
        // 名前は onclick に直接埋め込まず index を渡す（引用符を含む名前でも壊れない）。
        el.innerHTML = table(["取引先名", "操作"], rows.map((name, i) => [
          escapeHtml(name),
          `<button class="link" onclick="editPartner('${c.type}', ${i})">編集</button> `
          + `<button class="link" onclick="deletePartner('${c.type}', ${i})">削除</button>`
        ]));
      }
    }

    async function editPartner(type, index) {
      const oldName = partnerRowsFor(type)[index];
      if (!oldName) return;
      const input = prompt("取引先名を修正", oldName);
      if (input === null) return;
      const newName = input.trim();
      if (!newName || newName === oldName) return;
      try {
        const r = await api("/api/business-partners/update", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ partner_type: type, old_name: oldName, new_name: newName })
        });
        await loadAll();
        // Phase D⑥: 疑似freee へ送信済みの取引の取引先名も直したかを案内する。
        let suffix = "";
        if (r.partner_sync) {
          suffix = r.partner_sync.ok
            ? `／疑似freeeの送信済み取引 ${r.partner_sync.updated_deals}件も更新`
            : `／疑似freeeは未反映（${r.partner_sync.error}）。疑似freee起動後にもう一度編集すると同期します`;
        }
        document.getElementById("message").textContent = `取引先名を「${newName}」に修正しました（過去の取引も更新）${suffix}`;
      } catch (error) {
        document.getElementById("message").textContent = error.message;
      }
    }

    async function deletePartner(type, index) {
      const name = partnerRowsFor(type)[index];
      if (!name) return;
      if (!confirm(`取引先「${name}」を削除しますか？`)) return;
      try {
        await api("/api/business-partners/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ partner_type: type, partner_name: name })
        });
        await loadAll();
        document.getElementById("message").textContent = `取引先「${name}」を削除しました`;
      } catch (error) {
        document.getElementById("message").textContent = error.message;
      }
    }

    function renderPartnerSelects() {
      const configs = [
        { selector: "#purchaseForm select[name='partner_name']", rows: currentPartners.suppliers || [] },
        { selector: "#saleForm select[name='partner_name']", rows: currentPartners.customers || [] }
      ];
      for (const config of configs) {
        const select = document.querySelector(config.selector);
        if (!select) continue;
        const currentValue = select.value;
        select.innerHTML = config.rows
          .map(name => `<option value="${escapeAttr(name)}">${escapeHtml(name)}</option>`)
          .join("");
        if (currentValue && config.rows.includes(currentValue)) {
          select.value = currentValue;
        }
      }
    }

    async function addPartner(partnerType, inputId, formId) {
      const input = document.getElementById(inputId);
      const partnerName = input.value.trim();
      if (!partnerName) {
        document.getElementById("message").textContent = "取引先名を入力してください";
        return;
      }
      await api("/api/business-partners", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ partner_type: partnerType, partner_name: partnerName })
      });
      input.value = "";
      await loadPartners();
      const select = document.querySelector(`#${formId} select[name='partner_name']`);
      if (select) select.value = partnerName;
      document.getElementById("message").textContent = "取引先を追加しました";
    }

    function renderQueue(rows) {
      currentQueueRows = rows;
      const collapsed = rows.length > QUEUE_COLLAPSE_LIMIT && !queueExpanded;
      const shown = collapsed ? rows.slice(0, QUEUE_COLLAPSE_LIMIT) : rows;
      let html = table(["ID", "元データ", "区分", "状態", "操作"],
        shown.map(q => [
          q.id,
          queueSourceLabel(q),
          q.direction,
          queueStatusCell(q),
          queueActions(q)
        ]));
      if (rows.length > QUEUE_COLLAPSE_LIMIT) {
        html += collapsed
          ? `<button type="button" class="link" onclick="toggleQueueExpanded()">残り ${rows.length - QUEUE_COLLAPSE_LIMIT} 件を表示（全${rows.length}件）</button>`
          : `<button type="button" class="link" onclick="toggleQueueExpanded()">折りたたむ</button>`;
      }
      document.getElementById("queue").innerHTML = html;
      updateQueueBadge(rows);
    }

    function toggleQueueExpanded() {
      queueExpanded = !queueExpanded;
      renderQueue(currentQueueRows);
    }

    function queueStatusCell(q) {
      let s = q.status;
      if (q.retry_count > 0) s += ` <span style="color:var(--muted)">(失敗${q.retry_count}回)</span>`;
      if (q.sync_error_message) s += `<br><span class="error">${q.sync_error_message}</span>`;
      return s;
    }

    function updateQueueBadge(rows) {
      const unsent = rows.filter(q => ["pending", "failed", "retry"].includes(q.status)).length;
      const badge = document.getElementById("queueUnsentBadge");
      const btn = document.getElementById("sendAllBtn");
      if (badge) {
        if (unsent > 0) { badge.textContent = `未送信 ${unsent}件`; badge.style.display = ""; }
        else { badge.style.display = "none"; }
      }
      if (btn) { btn.disabled = unsent === 0; btn.style.opacity = unsent === 0 ? "0.5" : ""; }
    }

    function queueSourceLabel(q) {
      // Phase C: purchase_cancel / sale_cancel は在庫取消の「取消仕訳」（マイナス deal）。
      const cancelMap = { purchase_cancel: "仕入取消", sale_cancel: "売上取消" };
      if (cancelMap[q.source_type]) {
        return `<span class="status danger">取消仕訳</span> ${cancelMap[q.source_type]} #${q.source_id}`;
      }
      return `${q.source_type} #${q.source_id}`;
    }

    function queueActions(q) {
      const isPreviewOpen = currentPreviewKey === queuePreviewKey(q.source_type, q.source_id);
      const previewButton = `<button type="button" class="${isPreviewOpen ? "secondary" : ""}" aria-pressed="${isPreviewOpen}" onclick="togglePreview('${q.source_type}', ${q.source_id})">${isPreviewOpen ? "閉じる" : "確認"}</button>`;
      if (q.status === "sent") {
        return `${previewButton} <span class="match">送信済み ${q.external_accounting_id || ""}</span>`;
      }
      if (q.status === "cancelled") {
        // 取消済みは送信待ちから外しているため通常は表示されないが、念のため送信不可にする。
        return `${previewButton} <span class="status danger">取消済み（送信しません）</span>`;
      }
      const label = q.status === "failed" ? "再送" : "疑似freeeへ送信";
      return `${previewButton} <button class="warning" onclick="sendToPseudoFreee(${q.id})">${label}</button>`;
    }

    async function loadLedger(productId) {
      const data = await api(`/api/products/${productId}/ledger`);
      const product = data.product;
      currentLedgerProductId = product.id;
      currentLedgerData = data;
      ledgerExpanded = false;
      renderLedger();
    }

    function renderLedger() {
      if (!currentLedgerData) return;
      const product = currentLedgerData.product;
      const rows = currentLedgerData.ledger;
      const visibleRows = ledgerExpanded ? rows : rows.slice(0, 10);
      const ledgerSelect = document.getElementById("ledgerProductSelect");
      if (ledgerSelect && String(ledgerSelect.value) !== String(product.id)) {
        ledgerSelect.value = product.id;
      }
      document.getElementById("ledgerTitle").textContent = `${product.sku} ${product.product_name} の在庫元帳`;
      document.getElementById("ledgerNote").textContent = ledgerExpanded
        ? `全${rows.length}行を日付の新しい順に表示しています。この元帳の残高が、在庫一覧の現在庫として集計されています。現在の在庫残高金額は ${yen.format(product.inventory_balance_amount)} です。`
        : `最新${visibleRows.length}行のみを日付の新しい順に表示しています。全${rows.length}行を確認する場合は「すべて表示」を押してください。現在の在庫残高金額は ${yen.format(product.inventory_balance_amount)} です。`;
      document.getElementById("ledger").innerHTML = table(
        ["日付", "区分", "取引先", "請求書/注文番号", "入庫", "出庫", "残高", "単価", "取引金額", "在庫残高金額", "freee状態", "操作"],
        visibleRows.map(r => [
          r.movement_date,
          movementLabel(r.movement_type),
          r.partner_name || "-",
          r.invoice_no || "-",
          r.in_quantity || "",
          r.out_quantity || "",
          r.balance,
          yen.format(r.unit_price),
          yen.format(r.amount),
          yen.format(r.inventory_balance_amount),
          r.queue_status || r.accounting_status || "-",
          ledgerAction(r)
        ])
      ) + ledgerToggle(rows.length);
    }

    function ledgerToggle(totalRows) {
      if (totalRows <= 10) return "";
      const label = ledgerExpanded ? "最新10件のみ表示" : "すべて表示";
      return `<div class="table-total"><span>${ledgerExpanded ? "全件表示中" : "折りたたみ表示中"}</span><button class="secondary" onclick="toggleLedger()">${label}</button></div>`;
    }

    function toggleLedger() {
      ledgerExpanded = !ledgerExpanded;
      renderLedger();
    }

    function movementLabel(value) {
      return {
        initial_stock: "初期在庫",
        purchase_receipt: "仕入入庫",
        sale_shipment: "売上出庫",
        purchase_cancel: "仕入取消",
        sale_cancel: "売上取消"
      }[value] || value;
    }

    function ledgerAction(row) {
      if (row.is_correction) return "訂正行";
      if (row.is_cancelled) return "取消済み";
      if (row.source_type !== "purchase" && row.source_type !== "sale") return "-";
      return `<button class="warning" onclick="cancelMovement(${row.id})">取消</button>`;
    }

    async function cancelMovement(movementId) {
      const reason = prompt("取消理由を入力してください", "入力ミスのため取消");
      if (!reason) return;
      const result = await api("/api/inventory-movements/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ movement_id: movementId, reason })
      });
      document.getElementById("message").textContent = result.cancel_queued
        ? "元帳に取消行を追加しました。疑似freeeへ送信済みのため、取消仕訳を送信待ちキューに積みました（「疑似freeeへ送信」で反映してください）。"
        : "元帳に取消行を追加しました";
      await loadAll();
    }

    function table(headers, rows) {
      return `<table><thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    }

    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, char => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function escapeAttr(value) {
      return escapeHtml(value);
    }

    function status(text) {
      const cls = text === "正常" ? "ok" : (text === "欠品" ? "danger" : "warn");
      return `<span class="status ${cls}">${text}</span>`;
    }

    function showTransactionForm(formId) {
      document.querySelectorAll(".transaction-form").forEach(form => form.classList.toggle("active", form.id === formId));
      document.querySelectorAll(".form-tab").forEach(tab => tab.classList.toggle("active", tab.dataset.form === formId));
      const label = document.getElementById("dzKindLabel");
      if (label) label.textContent = formId === "saleForm" ? "売上" : "仕入";
    }

    async function submitForm(form, path) {
      const data = Object.fromEntries(new FormData(form).entries());
      const productId = data.product_id;
      const result = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
      document.getElementById("message").textContent = result.ok ? "登録しました" : "";
      form.reset();
      for (const input of form.querySelectorAll('input[type="date"][required]')) input.value = today;
      updateAmountSummary(form);
      await loadAll();
      await loadLedger(productId);
    }

    function queuePreviewKey(sourceType, sourceId) {
      return `${sourceType}:${sourceId}`;
    }

    function closePreview() {
      currentPreviewKey = null;
      document.getElementById("preview").textContent = defaultPreviewText;
      renderQueue(currentQueueRows);
    }

    async function togglePreview(sourceType, sourceId) {
      const key = queuePreviewKey(sourceType, sourceId);
      if (currentPreviewKey === key) {
        closePreview();
        return;
      }
      currentPreviewKey = key;
      renderQueue(currentQueueRows);
      const data = await api(`/api/freee-preview?source_type=${sourceType}&source_id=${sourceId}`);
      document.getElementById("preview").textContent = JSON.stringify(data, null, 2);
    }

    async function sendToPseudoFreee(id) {
      try {
        const result = await api("/api/freee-sync-queue/send", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id }) });
        document.getElementById("message").textContent = `疑似freeeへ送信しました: ${result.external_accounting_id}`;
      } catch (error) {
        document.getElementById("message").textContent = error.message;
      }
      await loadAll();
    }

    // Phase D①: 未送信（pending/failed/retry）をまとめて送る。失敗は retry_count++ で残り、再度押せばリトライ。
    async function sendAllToPseudoFreee() {
      const btn = document.getElementById("sendAllBtn");
      const msg = document.getElementById("message");
      if (btn) btn.disabled = true;
      try {
        const r = await api("/api/freee-sync-queue/send-all", { method: "POST" });
        if (r.attempted === 0) {
          msg.textContent = "未送信のキューはありません。";
        } else if (r.failed === 0) {
          msg.textContent = `一括送信: ${r.sent}件 送信しました。`;
        } else {
          let t = `一括送信: 成功 ${r.sent}件 / 失敗 ${r.failed}件（未送信 ${r.remaining_unsent}件 残り）。疑似freeeが起動しているか確認し、もう一度押すと再送します。`;
          if (r.errors && r.errors.length) t += " 例: " + r.errors.slice(0, 2).map(e => `${e.source_type}#${e.source_id}(${e.error})`).join(" / ");
          msg.textContent = t;
        }
      } catch (error) {
        msg.textContent = "一括送信に失敗しました: " + error.message;
      }
      await loadAll();
    }

    // 数量×単価で小計、税率で消費税（端数切り捨て=請求書の慣行）、合計＝小計＋消費税。
    function updateAmountSummary(form) {
      const box = form.querySelector(".amount-summary");
      if (!box) return;
      const qty = Number(form.quantity && form.quantity.value) || 0;
      const unit = Number(form.unit_price && form.unit_price.value) || 0;
      const rate = Number(form.tax_rate && form.tax_rate.value) || 0;
      const subtotal = qty * unit;
      const tax = Math.floor(subtotal * rate / 100);
      box.querySelector('[data-summary="subtotal"]').textContent = yen.format(subtotal);
      box.querySelector('[data-summary="tax"]').textContent = yen.format(tax);
      box.querySelector('[data-summary="total"]').textContent = yen.format(subtotal + tax);
    }
    // 数量・単価・税率の入力に追従して再計算する。
    document.querySelectorAll(".transaction-form").forEach(form => {
      ["quantity", "unit_price", "tax_rate"].forEach(name => {
        if (form[name]) form[name].addEventListener("input", () => updateAmountSummary(form));
      });
    });

    document.getElementById("purchaseForm").addEventListener("submit", async event => {
      event.preventDefault();
      try { await submitForm(event.target, "/api/purchases"); } catch (error) { document.getElementById("message").textContent = error.message; }
    });
    document.getElementById("saleForm").addEventListener("submit", async event => {
      event.preventDefault();
      try { await submitForm(event.target, "/api/sales"); } catch (error) { document.getElementById("message").textContent = error.message; }
    });

    // --- A-5: 経費キャプチャ（AI証憑入力） ----------------------------------
    // 認証付きで元画像を取得し、object URL を返す（<img src> では Bearer を付けられないため）。
    async function voucherImageObjectUrl(id) {
      const token = await getAuthToken();
      const headers = {};
      if (token) headers["Authorization"] = "Bearer " + token;
      const res = await fetch(`/api/vouchers/${id}/image`, { headers });
      if (!res.ok) throw new Error("画像の取得に失敗しました");
      return URL.createObjectURL(await res.blob());
    }

    function activeTransactionForm() {
      return document.querySelector(".transaction-form.active") || document.getElementById("purchaseForm");
    }
    function activeKind() {
      return activeTransactionForm().id === "saleForm" ? "sale" : "purchase";
    }
    function taxCategoryFor(kind, rate) {
      const r = Number(rate) === 8 ? "8%" : "10%";
      return (kind === "purchase" ? "課税仕入 " : "課税売上 ") + r;
    }
    function setSelectByValue(select, value) {
      // product_id を選ぶ。一致が無ければ何もしない（人が選ぶ）。
      if (value == null || value === "") return false;
      const v = String(value);
      if ([...select.options].some(o => o.value === v)) { select.value = v; return true; }
      return false;
    }
    function setSelectByText(select, text) {
      // 取引先select: 一致 option を選ぶ。無ければ一時 option を足して選ぶ（後で登録時にマスタ化）。
      if (!text) return false;
      if (![...select.options].some(o => o.value === text)) {
        select.insertAdjacentHTML("afterbegin", `<option value="${escapeAttr(text)}">${escapeHtml(text)}</option>`);
      }
      select.value = text;
      return true;
    }

    // 請求書フィールド名 → フォームの入力欄名（product_sku は商品選択に対応）
    const INVOICE_FIELD_TO_INPUT = {
      partner_name: "partner_name", invoice_no: "invoice_no", transaction_date: "transaction_date",
      product_sku: "product_id", quantity: "quantity", unit_price: "unit_price", tax_rate: "tax_rate"
    };
    function markInvoiceLowConfidence(form, lowFields) {
      form.querySelectorAll(".field-low").forEach(el => el.classList.remove("field-low"));
      (lowFields || []).forEach(f => {
        const input = form.querySelector(`[name="${INVOICE_FIELD_TO_INPUT[f] || f}"]`);
        if (input) input.classList.add("field-low");
      });
    }

    async function captureInvoice(file) {
      if (!file) return;
      const kind = activeKind();
      const form = activeTransactionForm();
      const dz = document.getElementById("invoiceDrop");
      const statusEl = document.getElementById("invoiceStatus");
      dz.classList.add("busy");
      statusEl.textContent = "AIが請求書を解析しています…";
      try {
        const body = new FormData();
        body.append("file", file);
        // BYO-key: ブラウザに保存した自分のキーがあれば、この解析リクエストにだけ添えて送る。
        const headers = {};
        const aiKey = getAnthropicKey();
        if (aiKey) headers["X-Anthropic-Key"] = aiKey;
        const result = await api(`/api/invoice-capture?kind=${kind}`, { method: "POST", body, headers });
        const d = result.draft;
        if (form.partner_name) setSelectByText(form.partner_name, d.partner_name);
        form.invoice_no.value = d.invoice_no || "";
        form.transaction_date.value = d.transaction_date || today;
        if (form.received_date) form.received_date.value = d.transaction_date || today;
        form.quantity.value = d.quantity || "";
        form.unit_price.value = d.unit_price || "";
        if (form.tax_rate) form.tax_rate.value = d.tax_rate || 10;
        if (form.tax_category) form.tax_category.value = taxCategoryFor(kind, d.tax_rate);
        updateAmountSummary(form);
        const matched = setSelectByValue(form.product_id, result.matched_product_id);
        form.voucher_id.value = result.voucher_id;
        markInvoiceLowConfidence(form, result.low_confidence_fields || []);
        if (!matched) form.product_id.classList.add("field-low"); // 商品が決まらなければ要選択
        const srcLabel = result.source === "anthropic" ? "Claude解析" : "デモ解析(スタブ)";
        statusEl.innerHTML = `${srcLabel}（全体信頼度 ${Math.round(result.overall_confidence * 100)}%）。<strong>⚠の項目を確認し、内容を直して「${kind === "purchase" ? "仕入" : "売上"}登録」を押してください。</strong>`;
        document.getElementById("invoiceImage").value = "";
        await loadVouchers();
      } catch (error) {
        statusEl.textContent = error.message;
      } finally {
        dz.classList.remove("busy");
      }
    }

    // 入力経路: ファイル選択 / ドラッグ&ドロップ / ペースト(Ctrl+V)
    const invoiceDrop = document.getElementById("invoiceDrop");
    document.getElementById("invoiceImage").addEventListener("change", e => captureInvoice(e.target.files[0]));
    ["dragenter", "dragover"].forEach(ev => invoiceDrop.addEventListener(ev, e => { e.preventDefault(); invoiceDrop.classList.add("dragover"); }));
    ["dragleave", "dragend"].forEach(ev => invoiceDrop.addEventListener(ev, () => invoiceDrop.classList.remove("dragover")));
    invoiceDrop.addEventListener("drop", e => {
      e.preventDefault();
      invoiceDrop.classList.remove("dragover");
      const file = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (file) captureInvoice(file);
    });
    window.addEventListener("paste", e => {
      const items = (e.clipboardData && e.clipboardData.items) || [];
      for (const it of items) {
        if (it.type && it.type.startsWith("image/")) { captureInvoice(it.getAsFile()); break; }
      }
    });

    // A-6 BYO-key: 自分の Anthropic キーをこのブラウザにだけ保存し、解析時にヘッダで都度送る。
    const AI_KEY_LS = "anthropic_api_key";
    function getAnthropicKey() {
      try { return (localStorage.getItem(AI_KEY_LS) || "").trim(); } catch (e) { return ""; }
    }
    function renderAiKeyStatus() {
      const has = !!getAnthropicKey();
      const statusEl = document.getElementById("aiKeyStatus");
      if (statusEl) {
        statusEl.innerHTML = has
          ? "🟢 AI解析：<strong>あなたのキーで有効</strong>（本物のClaudeで読み取り）"
          : "⚪ AI解析：<strong>サンプル動作中</strong>（自分のAnthropicキーを入れると本物のAIになります）";
      }
      const input = document.getElementById("anthropicKeyInput");
      if (input) input.placeholder = has ? "設定済み（変更する場合は貼り直し）" : "sk-ant-... を貼り付け";
    }
    const aiKeySaveBtn = document.getElementById("aiKeySave");
    if (aiKeySaveBtn) aiKeySaveBtn.addEventListener("click", () => {
      const input = document.getElementById("anthropicKeyInput");
      const v = (input.value || "").trim();
      try { if (v) localStorage.setItem(AI_KEY_LS, v); } catch (e) { /* localStorage 無効でも続行 */ }
      input.value = "";
      renderAiKeyStatus();
    });
    const aiKeyClearBtn = document.getElementById("aiKeyClear");
    if (aiKeyClearBtn) aiKeyClearBtn.addEventListener("click", () => {
      try { localStorage.removeItem(AI_KEY_LS); } catch (e) { /* no-op */ }
      const input = document.getElementById("anthropicKeyInput");
      if (input) input.value = "";
      renderAiKeyStatus();
    });
    renderAiKeyStatus();

    // A-9 実運用化: 売上履歴CSVの一括取込 と クリーンスタート（デモ全消去）。
    const salesCsvImportBtn = document.getElementById("salesCsvImportBtn");
    if (salesCsvImportBtn) salesCsvImportBtn.addEventListener("click", async () => {
      const fileInput = document.getElementById("salesCsvFile");
      const resultEl = document.getElementById("salesCsvResult");
      const file = fileInput && fileInput.files && fileInput.files[0];
      if (!file) { resultEl.textContent = "CSVファイルを選んでください。"; return; }
      resultEl.textContent = "取込中…";
      let r;
      try {
        const csv = await file.text();
        r = await api("/api/import/sales-history", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ csv })
        });
      } catch (e) {
        resultEl.textContent = "取込に失敗しました: " + e.message;
        return;
      }
      // ここに来たら取込API自体は成功（DBに入っている）。以降の画面再描画が失敗しても「取込失敗」とは出さない。
      let msg = `需要履歴に取込 ${r.imported}件・新規商品 ${r.created_products}件・スキップ ${r.skipped}件（予測用。在庫元帳・会計には計上しません）。`;
      if (r.errors && r.errors.length) {
        msg += " 例: " + r.errors.slice(0, 3).map(e => `${e.line}行目(${e.error})`).join(" / ");
      }
      msg += " → 下の「需要予測レベル2」で『予測バッチを実行』すると予測に反映されます。";
      resultEl.textContent = msg;
      try {
        await loadAll();
      } catch (e) {
        // 取込は成功済み。再描画だけ失敗した場合は、その旨を別表示（取込成功は覆さない）。
        resultEl.textContent = msg + "（画面の再読込に失敗しました。ページを再読み込みしてください: " + e.message + "）";
      }
    });

    const clearDataBtn = document.getElementById("clearDataBtn");
    if (clearDataBtn) clearDataBtn.addEventListener("click", async () => {
      if (!confirm("この組織の商品・取引・履歴・予測・証憑をすべて消去します。\nアカウントとログインは残ります。\n元に戻せません。よろしいですか？")) return;
      try {
        await api("/api/org/clear-data", { method: "POST" });
        document.getElementById("message").textContent = "データを全消去しました。実データで始められます。";
        await loadAll();
      } catch (e) {
        document.getElementById("message").textContent = "消去に失敗しました: " + e.message;
      }
    });

    // Phase D④: 期末在庫を疑似freee の決算へ送る（帳簿評価額の計算＋送信）。
    const closingCalcBtn = document.getElementById("closingCalcBtn");
    if (closingCalcBtn) closingCalcBtn.addEventListener("click", async () => {
      const period = (document.getElementById("closingPeriod").value || "").trim();
      const as_of = (document.getElementById("closingAsOf").value || "").trim();
      const el = document.getElementById("closingResult");
      try {
        const r = await api("/api/closing-inventory/preview", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ period, as_of }) });
        const when = as_of ? `（${as_of} 時点）` : "（現在）";
        const shrinkText = r.shrinkage_amount > 0
          ? ` ／ 棚卸減耗 ${yen.format(r.shrinkage_amount)}（帳簿−実地）→ 会計で棚卸減耗損に計上`
          : "（帳簿＝実地・減耗なし）";
        el.textContent = `帳簿棚卸高 ${yen.format(r.book_amount)} ／ 実地棚卸高 ${yen.format(r.physical_amount)}${shrinkText} ${when}`;
      } catch (e) {
        el.textContent = "計算に失敗しました: " + e.message;
      }
    });
    const shrinkBtn = document.getElementById("shrinkBtn");
    if (shrinkBtn) shrinkBtn.addEventListener("click", async () => {
      const product_id = (document.getElementById("shrinkProduct").value || "").trim();
      const physical_quantity = (document.getElementById("shrinkPhysicalQty").value || "").trim();
      const el = document.getElementById("shrinkResult");
      if (!product_id) { el.textContent = "商品を選んでください。"; return; }
      if (physical_quantity === "") { el.textContent = "実地数量を入力してください。"; return; }
      try {
        const r = await api("/api/shrinkage", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ product_id, physical_quantity }) });
        if (r.delta === 0) {
          el.textContent = "帳簿と実地が同じため、評価減はありませんでした。";
        } else if (r.delta < 0) {
          el.textContent = `評価減を記録しました（${-r.delta} 個の棚卸減耗）。在庫一覧・期末在庫に反映しました。上の「freeeへ送信」で会計へ連携できます。`;
        } else {
          el.textContent = `実地が帳簿より ${r.delta} 個多いため、在庫を増やす調整を記録しました。`;
        }
        await loadAll();
      } catch (e) {
        el.textContent = "記録に失敗しました: " + e.message;
      }
    });
    // Phase D⑤: 会計突合（在庫⇄疑似freee）をオンデマンドで実行する。
    async function runReconciliation() {
      const el = document.getElementById("reconResult");
      const badge = document.getElementById("reconBadge");
      el.textContent = "突合中…";
      let r;
      try {
        r = await api("/api/reconciliation");
      } catch (e) {
        el.textContent = "突合に失敗しました: " + e.message;
        return;
      }
      if (!r.freee_available) {
        badge.style.display = "none";
        el.innerHTML = '<p class="error">疑似freee に接続できません。疑似freee を起動してから再度突合してください。</p>';
        return;
      }
      badge.style.display = "";
      badge.className = "status " + (r.all_match ? "match" : "danger");
      badge.textContent = r.all_match ? "一致 ✓" : "差分あり";
      el.innerHTML = table(["項目", "在庫", "疑似freee", "差分", "判定"],
        r.rows.map(row => [
          row.label,
          yen.format(row.inventory),
          yen.format(row.freee),
          yen.format(row.diff),
          row.match ? '<span class="match">✓ 一致</span>' : `<span class="status danger">✗ 差分</span>`
        ]));
    }
    const reconBtn = document.getElementById("reconBtn");
    if (reconBtn) reconBtn.addEventListener("click", runReconciliation);

    const closingPushBtn = document.getElementById("closingPushBtn");
    if (closingPushBtn) closingPushBtn.addEventListener("click", async () => {
      const period = (document.getElementById("closingPeriod").value || "").trim();
      const as_of = (document.getElementById("closingAsOf").value || "").trim();
      const el = document.getElementById("closingResult");
      if (!period) { el.textContent = "対象期(YYYYMM)を入力してください（例 202603）。"; return; }
      try {
        const body = { period, as_of };
        const r = await api("/api/closing-inventory/push", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
        const diff = r.book_amount - r.physical_amount;
        const shrinkText = diff > 0 ? ` 棚卸減耗 ${yen.format(diff)} を棚卸減耗損として計上。` : "";
        el.textContent = `送信しました（${r.period}）: 帳簿棚卸高 ${yen.format(r.book_amount)} / 実地棚卸高 ${yen.format(r.physical_amount)}。${shrinkText}疑似freee の決算（商品・売上原価・BS）に反映されます。`;
        await loadClosingSends();
      } catch (e) {
        el.textContent = "送信に失敗しました: " + e.message;
      }
    });

    async function loadClosingSends() {
      const el = document.getElementById("closingSends");
      if (!el) return;
      let data;
      try {
        data = await api("/api/closing-inventory/sends");
      } catch (e) {
        el.innerHTML = `<p class="note">送信履歴を取得できません: ${e.message}</p>`;
        return;
      }
      const sends = (data && data.sends) || [];
      if (!sends.length) {
        el.innerHTML = `<p class="note">まだ送信履歴はありません。「freeeへ送信」すると、送信日時・対象期・帳簿/実地/棚卸減耗損がここに記録されます。</p>`;
        return;
      }
      el.innerHTML = table(["送信日時", "対象期", "帳簿棚卸高", "実地棚卸高", "棚卸減耗損"],
        sends.map(s => [
          (s.sent_at || "").replace("T", " ").slice(0, 16),
          s.period,
          yen.format(s.book_amount),
          yen.format(s.physical_amount),
          Number(s.shrinkage_amount || 0) > 0 ? `<span class="mismatch">${yen.format(s.shrinkage_amount)}</span>` : yen.format(0),
        ]));
    }

    let currentVouchers = [];
    let vouchersExpanded = false;
    const VOUCHER_COLLAPSE_LIMIT = 5;

    async function loadVouchers() {
      const el = document.getElementById("voucherList");
      if (!el) return; // 一覧の置き場所が未配置のときは何もしない（配置確定までの保護）
      currentVouchers = await api("/api/vouchers");
      vouchersExpanded = false;
      renderVouchers();
    }

    function renderVouchers() {
      const el = document.getElementById("voucherList");
      if (!el) return;
      const rows = currentVouchers;
      if (!rows.length) { el.innerHTML = `<p class="note">まだ証憑はありません。上の「登録」で請求書を取り込んでください。</p>`; return; }
      const visible = vouchersExpanded ? rows : rows.slice(0, VOUCHER_COLLAPSE_LIMIT);
      el.innerHTML = table(["証憑", "区分", "取引先(AI)", "金額(AI)", "信頼度", "状態", ""],
        visible.map(v => {
          const kindLabel = v.kind === "sale" ? "売上" : "仕入";
          const badge = v.registered ? `<span class="badge registered">取込済</span>` : `<span class="badge draft">未取込</span>`;
          const actions = `<button class="link" onclick="showVoucherDetail(${v.id})">詳細</button> <button class="danger-link" onclick="deleteVoucher(${v.id})">削除</button>`;
          return [escapeHtml(v.file_name), kindLabel, escapeHtml(v.partner_name || "-"), v.amount ? yen.format(v.amount) : "-", `${Math.round((v.confidence || 0) * 100)}%`, badge, actions];
        })) + voucherToggle(rows.length);
    }

    function voucherToggle(total) {
      if (total <= VOUCHER_COLLAPSE_LIMIT) return "";
      const label = vouchersExpanded ? `最新${VOUCHER_COLLAPSE_LIMIT}件のみ表示` : `すべて表示（全${total}件）`;
      const state = vouchersExpanded ? "全件表示中" : `最新${VOUCHER_COLLAPSE_LIMIT}件を表示中`;
      return `<div class="table-total"><span>${state}</span><button class="secondary" onclick="toggleVouchers()">${label}</button></div>`;
    }

    function toggleVouchers() {
      vouchersExpanded = !vouchersExpanded;
      renderVouchers();
    }

    async function deleteVoucher(id) {
      if (!window.confirm("この証憑を削除しますか？（元画像も削除されます）")) return;
      try {
        await api(`/api/vouchers/${id}`, { method: "DELETE" });
        document.getElementById("voucherDetail").innerHTML = "";
        await loadVouchers();
      } catch (error) {
        document.getElementById("voucherDetail").innerHTML = `<p class="note">${escapeHtml(error.message)}</p>`;
      }
    }

    function invoiceFieldsTable(fields, confidence, lowFields) {
      const labels = { partner_name: "取引先", invoice_no: "請求書番号", transaction_date: "取引日", product_sku: "商品SKU", quantity: "数量", unit_price: "単価(税抜)", tax_rate: "税率" };
      return table(["項目", "AI推定値", "信頼度"],
        Object.keys(labels).map(k => {
          const conf = confidence && confidence[k] != null ? Math.round(confidence[k] * 100) + "%" : "-";
          const low = (lowFields || []).includes(k);
          let val = fields[k];
          if (k === "unit_price" && val) val = yen.format(val);
          val = (val === 0 || val) ? String(val) : "-";
          return [labels[k], escapeHtml(val), low ? `<span class="low-confidence">${conf}</span>` : conf];
        }));
    }

    async function showVoucherDetail(id) {
      const detail = document.getElementById("voucherDetail");
      try {
        const v = await api(`/api/vouchers/${id}`);
        const ai = v.ai_extracted || {};
        const kindLabel = v.kind === "sale" ? "売上" : "仕入";
        let html = `<h3 class="sub">証憑詳細 #${v.id}（${kindLabel}・${v.registered ? "取込済" : "未取込"}）</h3>`;
        try { html += `<img class="voucher-thumb" src="${await voucherImageObjectUrl(id)}" alt="請求書画像">`; }
        catch (e) { html += `<p class="note">画像を表示できませんでした。</p>`; }
        html += `<p class="note">AI読み取り（${ai.source === "anthropic" ? "Claude" : "デモ"}）：</p>`;
        html += invoiceFieldsTable(ai.fields || {}, ai.confidence || {}, ai.low_confidence_fields || []);
        if (v.linked_source_type) {
          html += `<p class="note">取込先：${v.linked_source_type === "sale" ? "売上" : "仕入"} #${v.linked_source_id}（人が確認して登録済）</p>`;
        } else {
          html += `<p class="note">まだ仕入/売上に登録されていません（AIの下書きのみ）。</p>`;
        }
        html += `<p><button class="danger-link" onclick="deleteVoucher(${v.id})">この証憑を削除</button></p>`;
        detail.innerHTML = html;
      } catch (error) {
        detail.innerHTML = `<p class="note">${escapeHtml(error.message)}</p>`;
      }
    }
    // --- A-3: 認証ブートストラップ ------------------------------------------
    // Clerk 設定時はサインインを必須化し、未サインインならゲートを表示してアプリを止める。
    // dev モード（Clerk 未設定）のときはトークン無しでそのまま起動する。
    const APP_CONFIG = window.__APP_CONFIG__ || {};

    function clerkFrontendApi(publishableKey) {
      // 公開キー（pk_test_xxx / pk_live_xxx）の3つ目以降は frontend-api ホストの base64。
      const encoded = publishableKey.split("_").slice(2).join("_");
      try { return atob(encoded).replace(/\$$/, ""); } catch (e) { return ""; }
    }

    function loadClerkScript(publishableKey) {
      return new Promise((resolve, reject) => {
        const host = clerkFrontendApi(publishableKey);
        if (!host) { reject(new Error("Clerk 公開キーの形式が不正です")); return; }
        const script = document.createElement("script");
        script.async = true;
        script.crossOrigin = "anonymous";
        script.setAttribute("data-clerk-publishable-key", publishableKey);
        script.src = `https://${host}/npm/@clerk/clerk-js@5/dist/clerk.browser.js`;
        script.onload = resolve;
        script.onerror = () => reject(new Error("Clerk スクリプトの読み込みに失敗しました"));
        document.head.appendChild(script);
      });
    }

    async function startApp() {
      document.body.classList.remove("gated");
      document.getElementById("signInGate").classList.remove("show");
      await loadAll();
    }

    async function bootstrapAuth() {
      if (!APP_CONFIG.clerkConfigured || APP_CONFIG.devMode) {
        // dev モード（AUTH_DEV_MODE=true）または Clerk 未設定なら、サインイン不要で起動する。
        // dev モードは .env に Clerk の鍵が残っていても優先（ローカルで Clerk を使わず試せる）。
        // 本番(APP_ENV=production)では auth_dev_mode() が強制 false になるので影響しない。
        await startApp();
        return;
      }
      await loadClerkScript(APP_CONFIG.clerkPublishableKey);
      await window.Clerk.load();
      const renderAuthState = async () => {
        if (window.Clerk.user) {
          const badge = document.getElementById("roleBadge");
          badge.hidden = false; badge.textContent = "サインイン中";
          window.Clerk.mountUserButton(document.getElementById("clerk-user"));
          await startApp();
        } else {
          document.body.classList.add("gated");
          document.getElementById("signInGate").classList.add("show");
          window.Clerk.mountSignIn(document.getElementById("clerk-signin"));
        }
      };
      window.Clerk.addListener(renderAuthState);
      await renderAuthState();
    }

    bootstrapAuth().catch(error => {
      document.getElementById("message").textContent = error.message;
    });
  </script>
</body>
</html>
"""


def render_index(publishable_key: str = "", clerk_configured: bool = False, dev_mode: bool = False) -> str:
    """サーバ側の認証設定を埋め込んで HTML を返す。

    publishable_key はブラウザに出してよい公開キー。秘密キーは絶対に渡さない。
    clerk_configured=False かつ dev_mode=True のときは、トークン無しでそのまま起動する。
    """
    config = {
        "clerkPublishableKey": publishable_key,
        "clerkConfigured": bool(clerk_configured),
        "devMode": bool(dev_mode),
    }
    # </script> でテンプレートが壊れないよう、JSON 内の "/" をエスケープして埋め込む。
    config_json = json.dumps(config, ensure_ascii=False).replace("</", "<\\/")
    return _INDEX_TEMPLATE.replace("__APP_CONFIG_JSON__", config_json)
