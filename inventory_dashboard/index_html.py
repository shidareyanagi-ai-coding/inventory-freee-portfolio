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
      <button class="secondary" onclick="loadAll()">更新</button>
      <div id="clerk-user"></div>
    </div>
  </header>
  <main>
    <section class="metrics" id="metrics"></section>
    <section class="top-grid">
      <div class="panel">
        <h2>在庫一覧</h2>
        <div id="products"></div>
      </div>
      <div class="summary-box">
        <h3>今月仕入 商品別</h3>
        <div id="monthlyPurchases"></div>
      </div>
      <div class="summary-box">
        <h3>今月売上 商品別</h3>
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
            <label>仕入日</label><input type="date" name="transaction_date" required>
            <label>入庫日</label><input type="date" name="received_date" required>
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
    <section class="panel">
      <div class="section-head">
        <h2>適正在庫シミュレーション</h2>
        <label class="inline-control">予測期間
          <select id="forecastHorizon" onchange="loadForecast()">
            <option value="30">直近30日</option>
            <option value="60">直近60日</option>
            <option value="90">直近90日</option>
          </select>
        </label>
      </div>
      <p class="note" id="forecastNote">過去売上から月末販売数、リードタイム需要、必要在庫、推奨発注量を計算します。</p>
      <div id="forecastSimulation"></div>
    </section>
    <section class="panel" id="forecastMlSection">
      <div class="section-head">
        <h2>需要予測レベル2（実績×予測）</h2>
        <button type="button" id="runForecastBtn" onclick="runForecastBatch()">予測バッチを実行</button>
      </div>
      <p class="note" id="forecastMlNote">baseline / SARIMA / LightGBM をバックテスト(MAE/MAPE)で比較し、実績線＋予測線＋信頼区間(80%)を表示します。「予測バッチを実行」で再計算します。</p>
      <div class="forecast-controls">
        <label class="inline-control">商品
          <select id="forecastProduct" onchange="loadForecastChart()"></select>
        </label>
        <label class="inline-control">モデル
          <select id="forecastModel" onchange="loadForecastChart()"></select>
        </label>
      </div>
      <div class="chart-wrap"><canvas id="forecastChart"></canvas></div>
      <div class="forecast-grid">
        <div>
          <h3 class="sub">モデル精度（バックテスト・末尾28日／★=最良）</h3>
          <div id="forecastEvaluations"></div>
        </div>
        <div>
          <h3 class="sub">発注候補（今すぐ発注が必要な商品）</h3>
          <div id="forecastCandidates"></div>
        </div>
      </div>
    </section>
    <section class="bottom-grid">
      <div class="summary-box">
        <h2>freee送信待ちキュー</h2>
        <div id="queue"></div>
      </div>
      <div class="summary-box">
        <h2>送信前レビュー</h2>
        <pre id="preview">キューの「確認」を押すと、freee送信用の中間データを表示します。</pre>
      </div>
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
      const data = await api("/api/dashboard");
      renderMetrics(data);
      renderProducts(data.products);
      renderMonthlySummary("monthlyPurchases", data.monthly_purchases, data.monthly_purchase_total, "今月仕入 合計");
      renderMonthlySummary("monthlySales", data.monthly_sales, data.monthly_sales_total, "今月売上 合計");
      await loadForecast();
      renderSelects(data.products);
      await loadForecastML(data.products);
      await loadPartners();
      if (currentLedgerProductId) {
        await loadLedger(currentLedgerProductId);
      } else if (data.products.length && !document.getElementById("ledger").innerHTML) {
        await loadLedger(data.products[0].id);
      }
      const queue = await api("/api/freee-sync-queue");
      renderQueue(queue);
      await loadVouchers();
    }

    function renderMetrics(data) {
      window.dashboardStockTotal = Number(data.total_stock_value || 0);
      document.getElementById("metrics").innerHTML = [
        ["在庫総額", yen.format(data.total_stock_value), ""],
        ["商品数", data.product_count, ""],
        ["発注/欠品リスク", data.reorder_count, Number(data.reorder_count || 0) > 0 ? "risk-alert" : ""],
        ["今月仕入", yen.format(data.monthly_purchase_total), ""],
        ["今月売上", yen.format(data.monthly_sales_total), ""]
      ].map(([label, value, cls]) => `<div class="metric ${cls}"><span>${label}</span><strong>${value}</strong></div>`).join("");
    }

    function renderProducts(products) {
      const listTotal = products.reduce((sum, p) => sum + Number(p.stock_value || 0), 0);
      const dashboardTotal = Number(window.dashboardStockTotal || 0);
      const diff = listTotal - dashboardTotal;
      const diffText = diff === 0 ? `<span class="match">在庫総額と一致</span>` : `<span class="mismatch">差額 ${yen.format(diff)}</span>`;
      document.getElementById("products").innerHTML = table(["SKU", "商品", "必要水準", "現在在庫", "状態", "在庫金額", "推奨発注量"],
        products.map(p => [p.sku, `<button class="link" onclick="loadLedger(${p.id})">${p.product_name}</button>`, p.required_stock_level, p.stock_quantity, status(p.status), yen.format(p.stock_value), p.recommended_order_quantity]))
        + `<div class="table-total"><span>在庫一覧 合計</span><strong>${yen.format(listTotal)}</strong><span>${diffText}</span></div>`
        + `<p class="note">在庫一覧の必要水準・推奨発注量は、適正在庫シミュレーション（AIモデル予測）と同じ基準です。必要水準 = リードタイム需要(予測) + 安全在庫。</p>`;
    }

    function renderMonthlySummary(elementId, rows, total, totalLabel) {
      document.getElementById(elementId).innerHTML = table(["SKU", "商品", "数量", "金額"],
        rows.map(row => [row.sku, row.product_name, row.quantity, yen.format(row.amount)]))
        + `<div class="table-total"><span>${totalLabel}</span><strong>${yen.format(total || 0)}</strong></div>`;
    }

    async function loadForecast() {
      const horizon = document.getElementById("forecastHorizon").value;
      const data = await api(`/api/forecast-simulation?horizon_days=${horizon}`);
      renderForecast(data);
    }

    function renderForecast(data) {
      document.getElementById("forecastNote").textContent =
        `AIモデル(最良)の予測で ${data.month_end} までの需要を見込み、各商品の「必要在庫」と「今すぐ発注量」を出しています。必要在庫 = リードタイム需要(入荷までに売れる予測数) + 安全在庫。今すぐ発注量 = max(必要在庫 − 現在在庫, 0)。`;
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

    async function loadForecastML(products) {
      const select = document.getElementById("forecastProduct");
      const previous = select.value;
      select.innerHTML = products.map(p => `<option value="${p.id}">${p.sku} ${p.product_name}</option>`).join("");
      if (previous && [...select.options].some(o => o.value === previous)) select.value = previous;
      await refreshForecastModelOptions();
      await Promise.all([loadForecastChart(), loadForecastEvaluations(), loadForecastCandidates()]);
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

    async function loadForecastEvaluations() {
      const rows = await api("/api/forecast/evaluations");
      const target = document.getElementById("forecastEvaluations");
      if (!rows.length) {
        target.innerHTML = '<p class="note">まだバックテスト結果がありません。「予測バッチを実行」を押してください。</p>';
        return;
      }
      const best = rows[0].model_name; // MAE 昇順で返るため先頭が最良。
      target.innerHTML = table(["モデル", "期間", "MAE", "MAPE(%)"],
        rows.map(r => [
          modelLabel(r.model_name) + (r.model_name === best ? " ★" : ""),
          r.period,
          Number(r.mae).toFixed(2),
          Number.isFinite(r.mape) && r.mape > 0 ? Number(r.mape).toFixed(1) : "—",
        ]));
    }

    async function loadForecastCandidates() {
      const rows = await api("/api/forecast/order-candidates");
      const target = document.getElementById("forecastCandidates");
      if (!rows.length) {
        target.innerHTML = '<p class="note">発注候補はありません（予測上、在庫は当面足ります）。</p>';
        return;
      }
      target.innerHTML = table(["商品", "現在在庫", "必要在庫", "今すぐ発注量"],
        rows.map(r => [`${r.sku} ${r.product_name}`, r.stock_quantity, r.required_inventory, r.recommended_order_quantity]));
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
        await Promise.all([loadForecastChart(), loadForecastEvaluations(), loadForecastCandidates()]);
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
    }

    async function loadPartners() {
      currentPartners = await api("/api/business-partners");
      renderPartnerSelects();
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
      document.getElementById("queue").innerHTML = table(["ID", "元データ", "区分", "状態", "操作"],
        rows.map(q => [
          q.id,
          `${q.source_type} #${q.source_id}`,
          q.direction,
          q.sync_error_message ? `${q.status}<br><span class="error">${q.sync_error_message}</span>` : q.status,
          queueActions(q)
        ]));
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
      await api("/api/inventory-movements/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ movement_id: movementId, reason })
      });
      document.getElementById("message").textContent = "元帳に取消行を追加しました";
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
