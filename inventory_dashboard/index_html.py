# 在庫管理ダッシュボードのフロント (単一HTMLページ)。
# app.py(FastAPI) から読み込み、GET / で配信する。Plan A では既存HTMLをそのまま使う。

INDEX_HTML = r"""
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
    @media (max-width: 900px) {
      .metrics, .top-grid, .ledger-entry-grid, .bottom-grid { grid-template-columns: 1fr; }
      .entry-panel { position: static; }
      main { padding: 12px; }
      table { display: block; overflow-x: auto; }
    }
  </style>
</head>
<body>
  <header>
    <h1>在庫管理ダッシュボード</h1>
    <button class="secondary" onclick="loadAll()">更新</button>
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

    async function api(path, options = {}) {
      const res = await fetch(path, options);
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
      await loadPartners();
      if (currentLedgerProductId) {
        await loadLedger(currentLedgerProductId);
      } else if (data.products.length && !document.getElementById("ledger").innerHTML) {
        await loadLedger(data.products[0].id);
      }
      const queue = await api("/api/freee-sync-queue");
      renderQueue(queue);
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
    loadAll().catch(error => document.getElementById("message").textContent = error.message);
  </script>
</body>
</html>
"""
