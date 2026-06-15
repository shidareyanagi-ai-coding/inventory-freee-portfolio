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
    /* A-5: 経費キャプチャ（AI証憑入力） */
    .expense-grid { display: grid; grid-template-columns: minmax(0, 360px) minmax(0, 1fr); gap: 16px; align-items: start; }
    .expense-grid h3.sub { margin: 4px 0 8px; font-size: 14px; color: var(--muted); }
    .ai-pill { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px; background: #eef3f1; color: var(--accent); border: 1px solid #cfe0da; }
    label.low-confidence { color: var(--warn); font-weight: 700; }
    label.low-confidence::after { content: " ⚠ 要確認"; font-size: 11px; font-weight: 700; }
    .field-low { border-color: var(--warn); background: #fff8ec; }
    .voucher-thumb { max-width: 240px; max-height: 220px; border: 1px solid var(--line); border-radius: 6px; display: block; margin: 6px 0; }
    .badge { display: inline-block; font-size: 11px; padding: 2px 7px; border-radius: 4px; }
    .badge.registered { background: #e6f3ec; color: var(--ok); }
    .badge.draft { background: #fdeee9; color: var(--accent-2); }
    @media (max-width: 900px) {
      .metrics, .top-grid, .ledger-entry-grid, .bottom-grid, .forecast-grid, .expense-grid { grid-template-columns: 1fr; }
      .entry-panel { position: static; }
      main { padding: 12px; }
      table { display: block; overflow-x: auto; }
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
      <aside class="panel entry-panel">
        <h2>登録</h2>
        <div class="message" id="message"></div>
        <div class="form-tabs" role="tablist" aria-label="登録種別">
          <button class="form-tab active" type="button" data-form="purchaseForm" onclick="showTransactionForm('purchaseForm')">仕入</button>
          <button class="form-tab" type="button" data-form="saleForm" onclick="showTransactionForm('saleForm')">売上</button>
        </div>
        <div>
          <form id="purchaseForm" class="transaction-form active">
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
            <label>支払予定日</label><input type="date" name="due_date">
            <button type="submit">仕入登録</button>
          </form>
          <form id="saleForm" class="transaction-form">
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
          <h3 class="sub">発注候補（予測で在庫が必要水準を割る商品）</h3>
          <div id="forecastCandidates"></div>
        </div>
      </div>
    </section>
    <section class="panel" id="expenseCaptureSection">
      <div class="section-head">
        <h2>経費キャプチャ（AI証憑入力）</h2>
        <span class="ai-pill" id="captureSourcePill" hidden></span>
      </div>
      <p class="note">レシート・請求書の画像をアップロード（スマホはカメラ撮影）すると、AIが発生日・支払先・金額・税区分・勘定科目・摘要を推定して下書きに反映します。<strong>AIは下書きまで。「登録」はあなたが確認して押します（自動登録はしません）。</strong></p>
      <div class="expense-grid">
        <div>
          <form id="expenseCaptureForm">
            <label>証憑画像（レシート / 請求書）</label>
            <input type="file" id="expenseImage" accept="image/*" capture="environment" required>
            <button type="submit" id="captureBtn">AIで解析して下書きを作る</button>
          </form>
          <p class="note" id="expenseCaptureStatus"></p>
          <form id="expenseDraftForm" hidden>
            <h3 class="sub">下書き（確認・修正して登録）</h3>
            <label data-field="issue_date">発生日</label><input type="date" name="issue_date">
            <label data-field="partner_name">支払先</label><input name="partner_name" required>
            <label data-field="amount">金額（税込）</label><input type="number" name="amount" min="1" required>
            <label data-field="tax_category">税区分</label><select name="tax_category"></select>
            <label data-field="account_item">勘定科目</label><select name="account_item"></select>
            <label data-field="memo">摘要</label><input name="memo">
            <button type="submit" id="registerVoucherBtn">この内容で登録</button>
          </form>
        </div>
        <div>
          <h3 class="sub">証憑一覧（元画像・AI抽出・人の修正後を後から確認）</h3>
          <div id="voucherList"></div>
          <div id="voucherDetail"></div>
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
    let currentDraftVoucherId = null;
    let voucherCandidates = { account_items: [], tax_categories: [] };
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
        + `<p class="note">在庫一覧の必要水準は、適正在庫シミュレーションと同じ直近30日予測ベースです。必要水準 = リードタイム需要 + 安全在庫。推奨発注量 = max(必要水準 - 現在在庫, 0) です。</p>`;
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
        `${data.start_date} から ${data.end_date} までの販売実績を使い、${data.month_end} までの残り ${data.days_to_month_end} 日を予測しています。リードタイム需要は「発注から入荷までに売れそうな数量」、必要在庫は「リードタイム需要 + 安全在庫」です。`;
      document.getElementById("forecastSimulation").innerHTML = table(
        ["SKU", "商品", "現在在庫", "期間販売数", "日次平均", "季節係数", "リードタイム日数", "リードタイム需要", "安全在庫", "必要在庫", "今すぐ推奨発注量", "リードタイム判定", "月末までの予測販売数", "月末在庫見込み", "月末不足数", "月末判定"],
        data.rows.map(row => [
          row.sku,
          row.product_name,
          row.stock_quantity,
          row.recent_sales_quantity,
          row.daily_average,
          row.seasonal_factor,
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
      target.innerHTML = table(["商品", "必要水準を割る日", "推奨発注量", "根拠"],
        rows.map(r => [`${r.sku} ${r.product_name}`, r.suggested_date, r.recommended_quantity, r.basis]));
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
    }

    async function submitForm(form, path) {
      const data = Object.fromEntries(new FormData(form).entries());
      const productId = data.product_id;
      const result = await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
      document.getElementById("message").textContent = result.ok ? "登録しました" : "";
      form.reset();
      for (const input of form.querySelectorAll('input[type="date"][required]')) input.value = today;
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

    function fillSelect(select, options, value) {
      select.innerHTML = options.map(o => `<option value="${o}">${o}</option>`).join("");
      if (value && !options.includes(value)) select.insertAdjacentHTML("afterbegin", `<option value="${value}">${value}</option>`);
      select.value = value || (options[0] || "");
    }

    function markLowConfidence(lowFields) {
      const form = document.getElementById("expenseDraftForm");
      for (const label of form.querySelectorAll("label[data-field]")) {
        const field = label.dataset.field;
        const low = lowFields.includes(field);
        label.classList.toggle("low-confidence", low);
        const input = form.querySelector(`[name="${field}"]`);
        if (input) input.classList.toggle("field-low", low);
      }
    }

    async function captureExpense(event) {
      event.preventDefault();
      const fileInput = document.getElementById("expenseImage");
      const file = fileInput.files[0];
      if (!file) return;
      const status = document.getElementById("expenseCaptureStatus");
      const btn = document.getElementById("captureBtn");
      btn.disabled = true; status.textContent = "AIが画像を解析しています…";
      try {
        const body = new FormData();
        body.append("file", file);
        const result = await api("/api/expense-capture", { method: "POST", body });
        voucherCandidates = { account_items: result.account_item_candidates, tax_categories: result.tax_category_candidates };
        currentDraftVoucherId = result.voucher_id;
        const draft = result.draft;
        const form = document.getElementById("expenseDraftForm");
        form.issue_date.value = draft.issue_date || today;
        form.partner_name.value = draft.partner_name || "";
        form.amount.value = draft.amount || "";
        form.memo.value = draft.memo || "";
        fillSelect(form.tax_category, voucherCandidates.tax_categories, draft.tax_category);
        fillSelect(form.account_item, voucherCandidates.account_items, draft.account_item);
        markLowConfidence(result.low_confidence_fields || []);
        form.hidden = false;
        const pill = document.getElementById("captureSourcePill");
        pill.hidden = false;
        pill.textContent = `${result.source === "anthropic" ? "Claude解析" : "デモ解析(スタブ)"} ・ 全体信頼度 ${Math.round(result.overall_confidence * 100)}%`;
        const lows = (result.low_confidence_fields || []).length;
        status.textContent = lows ? `下書きを作成しました。⚠ の${lows}項目は信頼度が低めです。確認して登録してください。` : "下書きを作成しました。確認して登録してください。";
        await loadVouchers();
      } catch (error) {
        status.textContent = error.message;
      } finally {
        btn.disabled = false;
      }
    }

    async function registerVoucher(event) {
      event.preventDefault();
      if (!currentDraftVoucherId) return;
      const form = document.getElementById("expenseDraftForm");
      const payload = Object.fromEntries(new FormData(form).entries());
      const status = document.getElementById("expenseCaptureStatus");
      try {
        await api(`/api/vouchers/${currentDraftVoucherId}/register`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
        status.textContent = "登録しました（人が確認のうえ登録）。";
        form.hidden = true;
        document.getElementById("captureSourcePill").hidden = true;
        document.getElementById("expenseCaptureForm").reset();
        const registeredId = currentDraftVoucherId;
        currentDraftVoucherId = null;
        await loadVouchers();
        await showVoucherDetail(registeredId);
      } catch (error) {
        status.textContent = error.message;
      }
    }

    async function loadVouchers() {
      const rows = await api("/api/vouchers");
      const el = document.getElementById("voucherList");
      if (!rows.length) { el.innerHTML = `<p class="note">まだ証憑はありません。上のフォームから画像を解析してください。</p>`; return; }
      el.innerHTML = table(["証憑", "支払先(AI)", "金額(AI)", "信頼度", "状態", ""],
        rows.map(v => {
          const ai = (v.ai_extracted && v.ai_extracted.fields) || {};
          const badge = v.registered ? `<span class="badge registered">登録済</span>` : `<span class="badge draft">下書き</span>`;
          return [v.file_name, ai.partner_name || "-", ai.amount ? yen.format(ai.amount) : "-", `${Math.round((v.confidence || 0) * 100)}%`, badge, `<button class="link" onclick="showVoucherDetail(${v.id})">詳細</button>`];
        }));
    }

    function draftFieldsTable(fields, confidence, lowFields) {
      const labels = { issue_date: "発生日", partner_name: "支払先", amount: "金額", tax_category: "税区分", account_item: "勘定科目", memo: "摘要" };
      return table(["項目", "AI推定値", "信頼度"],
        Object.keys(labels).map(k => {
          const conf = confidence && confidence[k] != null ? Math.round(confidence[k] * 100) + "%" : "-";
          const low = (lowFields || []).includes(k);
          const val = k === "amount" && fields[k] ? yen.format(fields[k]) : (fields[k] || "-");
          return [labels[k], val, low ? `<span class="low-confidence">${conf}</span>` : conf];
        }));
    }

    async function showVoucherDetail(id) {
      const detail = document.getElementById("voucherDetail");
      try {
        const v = await api(`/api/vouchers/${id}`);
        const ai = v.ai_extracted || {};
        let html = `<h3 class="sub">証憑詳細 #${v.id}（${v.registered ? "登録済" : "下書き"}）</h3>`;
        try { html += `<img class="voucher-thumb" src="${await voucherImageObjectUrl(id)}" alt="証憑画像">`; }
        catch (e) { html += `<p class="note">画像を表示できませんでした。</p>`; }
        html += `<p class="note">AI抽出（${ai.source === "anthropic" ? "Claude" : "デモ"}）：</p>`;
        html += draftFieldsTable(ai.fields || {}, ai.confidence || {}, ai.low_confidence_fields || []);
        if (v.user_corrected) {
          html += `<p class="note">人の修正後（登録内容）：</p>`;
          html += draftFieldsTable(v.user_corrected, null, []);
        } else {
          html += `<p class="note">まだ登録されていません（AIの下書きのみ）。</p>`;
        }
        detail.innerHTML = html;
      } catch (error) {
        detail.innerHTML = `<p class="note">${error.message}</p>`;
      }
    }

    document.getElementById("expenseCaptureForm").addEventListener("submit", captureExpense);
    document.getElementById("expenseDraftForm").addEventListener("submit", registerVoucher);
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
      if (!APP_CONFIG.clerkConfigured) {
        // dev モード: 認証なしでそのまま起動（サーバ側 AUTH_DEV_MODE が許可）。
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
