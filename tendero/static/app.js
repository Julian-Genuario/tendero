"use strict";
const $ = (id) => document.getElementById(id);
const state = { cfg: null, cust: null, merch: null, lastCust: 0, lastMerch: 0, lastEvent: 0, grid: null, busy: {} };

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok && r.status !== 400) throw new Error(`${path} -> ${r.status}`);
  return r.json();
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" });

function storageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function storageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } }

// ------------------------------------------------------------------ chat rendering
function renderMsg(thread, m) {
  const el = document.createElement("div");
  el.className = `msg ${m.role}`;
  el.dataset.id = m.id;
  el.innerHTML = esc(m.text);
  if (m.tools && m.tools.length) {
    const t = document.createElement("div");
    t.className = "tools";
    t.innerHTML = m.tools.map((x) => `<span class="tool ${x.ok ? "ok" : "err"}" title="${esc(x.error || JSON.stringify(x.input))}">${esc(x.name)}</span>`).join("");
    el.appendChild(t);
  }
  if (m.card && m.card.type === "pay") {
    const c = document.createElement("div");
    c.className = "paycard";
    c.innerHTML = `<div class="amt">${esc(m.card.total)} ${esc(m.card.currency)}</div>
      <button class="paybtn" data-url="${esc(m.card.url)}">Pay with PayPal</button>
      <div class="meta">order ${esc(m.card.order_id)} · PayPal ${esc(m.card.paypal_order_id)}</div>`;
    c.querySelector("button").addEventListener("click", () => openCheckout(m.card.url));
    el.appendChild(c);
  }
  if (m.card && m.card.type === "paid") {
    const c = document.createElement("div");
    c.className = "paidcard";
    c.textContent = `✓ Verified payment · ${m.card.total} ${m.card.currency} · ${m.card.order_id}`;
    el.appendChild(c);
  }
  thread.appendChild(el);
  thread.scrollTop = thread.scrollHeight;
}

function setTyping(thread, on) {
  let t = thread.querySelector(".typing");
  if (on) {
    if (!t) {
      t = document.createElement("div");
      t.className = "typing";
      t.textContent = "assistant is working…";
    }
    thread.appendChild(t);
    thread.scrollTop = thread.scrollHeight;
  } else if (t) t.remove();
}

const pulling = {};
async function pullMessages(kind) {
  const cid = kind === "cust" ? state.cust : state.merch;
  const key = kind === "cust" ? "lastCust" : "lastMerch";
  const thread = kind === "cust" ? $("custThread") : $("mThread");
  if (!cid || pulling[kind]) return;
  pulling[kind] = true;
  try {
    const { messages } = await api(`/api/conversations/${cid}/messages?after=${state[key]}`);
    for (const m of messages) {
      if (m.id <= state[key]) continue;
      renderMsg(thread, m);
      state[key] = m.id;
      if (kind === "merch" && m.card) onMerchantCard(m.card);
    }
    setTyping(thread, !!state.busy[kind]);
  } finally { pulling[kind] = false; }
}

async function send(kind, text) {
  if (!text.trim() || state.busy[kind]) return;
  state.busy[kind] = true;
  const thread = kind === "cust" ? $("custThread") : $("mThread");
  const cid = kind === "cust" ? state.cust : state.merch;
  const p = api("/api/chat", { method: "POST", body: JSON.stringify({ conversation_id: cid, text }) });
  setTimeout(() => pullMessages(kind), 60);
  try { await p; } finally {
    state.busy[kind] = false;
    while (pulling[kind]) await new Promise((r) => setTimeout(r, 30));
    await pullMessages(kind);
    refreshMerchant();
  }
}

// ------------------------------------------------------------------ checkout
function openCheckout(url) {
  if (state.cfg.paypal.mode === "mock") {
    $("checkoutFrame").src = url;
    $("checkout").hidden = false;
  } else {
    window.open(url, "paypal_checkout", "width=480,height=720");
  }
}
function closeCheckout() { $("checkout").hidden = true; $("checkoutFrame").src = "about:blank"; }
window.addEventListener("message", (e) => {
  if (e.data && e.data.tendero === "checkout-done") setTimeout(closeCheckout, 1400);
});

// ------------------------------------------------------------------ merchant console
const STATUS_ORDER = ["PAID", "CAPTURED", "APPROVED", "AWAITING_PAYMENT", "DRAFT", "MISMATCH", "REFUNDED", "FAILED"];
const num = (v) => (v == null || v === "" ? null : parseFloat(v));
const fmt2 = (v) => (v == null ? "—" : Number(v).toFixed(2));
let gridTheme = null;
function theme() {
  // One Tendero theme for every grid, built with AG Grid's Theming API
  gridTheme = gridTheme || agGrid.themeQuartz.withParams({
    accentColor: "#0f766e", foregroundColor: "#161a21", fontFamily: "Inter, system-ui, sans-serif", fontSize: 12.5,
    headerFontWeight: 600, headerBackgroundColor: "#fafbfc", headerTextColor: "#4b5563", rowHeight: 34,
    headerHeight: 32, borderColor: "#eceef2", wrapperBorder: false, wrapperBorderRadius: 0,
    rowHoverColor: "#f3faf8", selectedRowBackgroundColor: "#e6f4f2", oddRowBackgroundColor: "#ffffff",
    spacing: 6, cellHorizontalPadding: 10,
  });
  return gridTheme;
}
const NO_ROWS = (t) => `<span style="color:#8a93a1">${t}</span>`;

function initGrid() {
  const cols = [
    { field: "id", headerName: "Order", width: 94, cellClass: "mono", pinned: "left" },
    { field: "customer_name", headerName: "Customer", flex: 1, minWidth: 110 },
    { field: "status", headerName: "Status", width: 168, filter: "agTextColumnFilter",
      cellRenderer: (p) => `<span class="pill ${p.value}">${p.value.replace("_", " ")}</span>`,
      comparator: (a, b) => STATUS_ORDER.indexOf(a) - STATUS_ORDER.indexOf(b) },
    { colId: "items", headerName: "Items", flex: 1.3, minWidth: 140, sortable: false, tooltipValueGetter: (p) => p.value,
      valueGetter: (p) => p.data.cart.lines.map((l) => `${l.quantity}× ${l.name.split(" (")[0]}`).join(", ") },
    { colId: "total", field: "total", headerName: "Total", width: 88, type: "rightAligned", filter: "agNumberColumnFilter",
      valueGetter: (p) => num(p.data.total), valueFormatter: (p) => fmt2(p.value) },
    { field: "paypal_fee", headerName: "Fee", width: 72, type: "rightAligned", valueFormatter: (p) => fmt2(p.value) },
    { field: "net_amount", headerName: "Net", width: 80, type: "rightAligned", valueFormatter: (p) => fmt2(p.value),
      cellClass: "strong" },
    { field: "paid_via", headerName: "Confirmed by", flex: 1, minWidth: 130, valueFormatter: (p) => p.value || "—" },
    { field: "paypal_order_id", headerName: "PayPal order", width: 160, cellClass: "mono dim", valueFormatter: (p) => p.value || "—" },
  ];
  state.grid = agGrid.createGrid($("ordersGrid"), {
    theme: theme(), columnDefs: cols, rowData: [], getRowId: (p) => p.data.id, animateRows: true,
    defaultColDef: { sortable: true, resizable: true }, tooltipShowDelay: 300,
    rowClassRules: { "row-alert": (p) => p.data && ["MISMATCH", "FAILED"].includes(p.data.status) },
    overlayNoRowsTemplate: NO_ROWS("No orders yet - start chatting on the left."),
    onFilterChanged: () => { if (!state.applyingView && !state.grid.isAnyFilterPresent()) $("viewChip").hidden = true; },
  });
  $("quickFilter").addEventListener("input", (e) => state.grid.setGridOption("quickFilterText", e.target.value));
  $("viewChipClear").addEventListener("click", clearGridView);
}

// The owner copilot drives the grid: its show_orders tool returns a view that we apply here.
const STATUS_LABEL = (s) => s.replace("_", " ").toLowerCase();
async function applyGridView(v) {
  showTab("orders");
  state.applyingView = true;
  const model = {};
  if (v.status && v.status !== "ANY") model.status = { filterType: "text", type: "equals", filter: v.status };
  if (v.min_total) model.total = { filterType: "number", type: "greaterThanOrEqual", filter: parseFloat(v.min_total) };
  await refreshMerchant();
  state.grid.setFilterModel(Object.keys(model).length ? model : null);
  const sort = { newest: null, total_desc: "desc", total_asc: "asc" }[v.sort];
  state.grid.applyColumnState({ state: [{ colId: "total", sort }], defaultState: { sort: null } });
  state.applyingView = false;
  const parts = [v.status && v.status !== "ANY" ? STATUS_LABEL(v.status) : "all orders"];
  if (v.min_total) parts.push(`≥ ${v.min_total}`);
  if (sort) parts.push(sort === "desc" ? "largest first" : "smallest first");
  $("viewChipText").textContent = `Copilot view: ${parts.join(" · ")} (${v.count})`;
  $("viewChip").hidden = false;
  const ids = [];
  state.grid.forEachNodeAfterFilter((n) => ids.push(n));
  state.grid.flashCells({ rowNodes: ids, columns: ["status", "total"] });
}
function clearGridView() {
  state.grid.setFilterModel(null);
  state.grid.applyColumnState({ defaultState: { sort: null } });
  $("viewChip").hidden = true;
}

// ------------------------------------------------------------------ inventory (AG Grid + AG Charts)
function initInventory() {
  const bar = (p) => {
    const d = p.data; const used = d.sold + d.reserved;
    const pct = (x) => Math.max(0, Math.min(100, (x / d.stock) * 100)).toFixed(1);
    return `<div class="stockbar" title="${d.sold} sold · ${d.reserved} reserved · ${d.available} available of ${d.stock}">
      <i class="s" style="width:${pct(d.sold)}%"></i><i class="r" style="width:${pct(d.reserved)}%"></i></div>
      <span class="stocknum ${d.level}">${d.available}<small>/${d.stock}</small></span>`;
  };
  state.inv = agGrid.createGrid($("inventoryGrid"), {
    theme: theme(), rowData: [], getRowId: (p) => p.data.sku, animateRows: true,
    defaultColDef: { sortable: true, resizable: true },
    rowClassRules: { "row-low": (p) => p.data && p.data.level === "low", "row-out": (p) => p.data && p.data.level === "out" },
    columnDefs: [
      { field: "sku", headerName: "SKU", width: 90, cellClass: "mono" },
      { field: "product", headerName: "Product", flex: 1.3, minWidth: 130, tooltipField: "product" },
      { field: "price", headerName: "Price", width: 70, type: "rightAligned" },
      { field: "sold", headerName: "Sold", width: 62, type: "rightAligned", enableCellChangeFlash: true },
      { field: "reserved", headerName: "In checkout", width: 92, type: "rightAligned", enableCellChangeFlash: true },
      { field: "available", headerName: "Available", flex: 1, minWidth: 120, cellRenderer: bar },
      { field: "revenue", headerName: "Revenue", width: 86, type: "rightAligned", cellClass: "strong",
        valueGetter: (p) => num(p.data.revenue), valueFormatter: (p) => fmt2(p.value), enableCellChangeFlash: true },
    ],
  });
  state.chart = agCharts.AgCharts.create({
    container: $("salesChart"), data: [], background: { visible: false },
    title: { text: "Units by product", fontSize: 12.5, fontWeight: 600, fontFamily: "Inter", color: "#161a21" },
    padding: { top: 6, right: 10, bottom: 4, left: 4 },
    series: [
      { type: "bar", direction: "horizontal", xKey: "short", yKey: "sold", yName: "Sold (PayPal verified)", stacked: true, fill: "#0f766e", cornerRadius: 3 },
      { type: "bar", direction: "horizontal", xKey: "short", yKey: "reserved", yName: "In checkout", stacked: true, fill: "#f0b25a", cornerRadius: 3 },
    ],
    axes: {
      x: { type: "category", position: "left", label: { fontSize: 11, fontFamily: "Inter", color: "#4b5563" } },
      y: { type: "number", position: "bottom", label: { fontSize: 10, color: "#8a93a1" }, gridLine: { style: [{ stroke: "#eef0f3" }] } },
    },
    legend: { position: "bottom", item: { label: { fontSize: 11, fontFamily: "Inter", color: "#4b5563" }, marker: { size: 9 } } },
  });
}
let invSig = "";
async function refreshInventory() {
  if (!state.inv) return;
  const { rows } = await api("/api/merchant/inventory");
  const sig = JSON.stringify(rows);
  if (sig === invSig) return;
  invSig = sig;
  state.inv.setGridOption("rowData", rows);
  const short = { "MATE-CAL": "Gourd", "BOMB-ALP": "Bombilla", "YERBA-1K": "Yerba 1 kg", "CASE-LTH": "Travel case", "ALF-12": "Alfajores" };
  state.chart.updateDelta({ data: rows.map((r) => ({ ...r, short: short[r.sku] || r.sku })) });
}

// ------------------------------------------------------------------ fulfillment (Bryntum Scheduler)
function iso(d) { // Bryntum gives local Date objects; the server speaks shop-local wall-clock time
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:00`;
}
function boardToast(text, bad) {
  const t = $("boardToast");
  t.textContent = text; t.className = `toast ${bad ? "bad" : "good"}`; t.hidden = false;
  clearTimeout(state.toastT); state.toastT = setTimeout(() => { t.hidden = true; }, 4200);
}
async function saveMove(record, startDate, endDate) {
  const r = await fetch(`/api/merchant/fulfillment/${record.id}`, {
    method: "PATCH", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ startDate: iso(startDate), endDate: iso(endDate) }),
  });
  const body = await r.json();
  if (!body.ok) boardToast(`Can't move it: ${body.error}`, true);
  else boardToast(`Saved: ${record.name} → ${bryntum.DateHelper.format(startDate, "ddd D MMM HH:mm")}`, false);
  return !!body.ok;
}
function initBoard() {
  if (state.board) return;
  const { Scheduler, DateHelper, StringHelper } = bryntum;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  state.board = new Scheduler({
    appendTo: "board", startDate: today, endDate: DateHelper.add(today, 5, "day"),
    viewPreset: { base: "hourAndDay", tickWidth: 58, displayDateFormat: "ddd D MMM HH:mm",
      headers: [{ unit: "day", dateFormat: "ddd D MMM" }, { unit: "hour", dateFormat: "HH" }] },
    // only workshop hours (Mon-Sat 9:00-19:00) on the time axis
    timeAxis: { continuous: false, generateTicks(start, end, unit, increment) {
      const ticks = [];
      for (let t = start; t < end; t = DateHelper.add(t, increment, unit)) {
        if (t.getDay() !== 0 && t.getHours() >= 9 && t.getHours() < 19) ticks.push({ startDate: t, endDate: DateHelper.add(t, increment, unit) });
      }
      return ticks;
    } },
    rowHeight: 46, barMargin: 7, eventStyle: "filled",
    resourceImagePath: null, createEventOnDblClick: false, zoomOnMouseWheel: false,
    columns: [{ type: "resourceInfo", text: "Workshop", field: "name", width: 170, showImage: false,
      showEventCount: false, htmlEncode: false, renderer: ({ record }) => StringHelper.xss`<div class="res"><b>${record.name}</b><small>${record.role}</small></div>` }],
    features: {
      dependencies: { radius: 4, clickWidth: 5 },
      eventDrag: { constrainDragToResource: true },
      eventResize: true, eventEdit: false, eventMenu: false, scheduleMenu: false, eventDragCreate: false,
      eventTooltip: { template: ({ eventRecord: e }) => StringHelper.xss`<div class="tip"><b>${e.name}</b><br>${e.detail}<br>${DateHelper.format(e.startDate, "ddd D MMM, HH:mm")} - ${DateHelper.format(e.endDate, "HH:mm")} (Buenos Aires)</div>` },
    },
    eventRenderer: ({ eventRecord: e }) => StringHelper.xss`${e.orderId} · ${e.kind === "qa" ? "check" : e.kind === "pack" ? "pack" : "pickup"}`,
    listeners: {
      beforeEventDropFinalize({ context }) {
        context.async = true;
        saveMove(context.eventRecord, context.startDate, context.endDate).then((ok) => context.finalize(ok));
      },
      beforeEventResizeFinalize({ context }) {
        context.async = true;
        saveMove(context.eventRecord, context.startDate, context.endDate).then((ok) => context.finalize(ok));
      },
    },
  });
  refreshBoard(true);
}
let boardSig = "";
async function refreshBoard(force) {
  if (!state.board) return;
  const b = await api("/api/merchant/fulfillment");
  const sig = JSON.stringify([b.events, b.dependencies]);
  if (!force && sig === boardSig) return;
  boardSig = sig;
  state.board.resources = b.resources;
  state.board.events = b.events.map((e) => ({ ...e, startDate: new Date(e.startDate), endDate: new Date(e.endDate) }));
  state.board.dependencies = b.dependencies;
  if (b.events.length) {
    const first = new Date(b.events.map((e) => e.startDate).sort()[0]);
    const day = new Date(first); day.setHours(0, 0, 0, 0);
    state.board.setTimeSpan(day, bryntum.DateHelper.add(day, 5, "day"));
    state.board.scrollToDate(bryntum.DateHelper.add(first, -1, "hour"), { block: "start", animate: 300 });
  }
}

// ------------------------------------------------------------------ market prices (Channel3 + AG Grid)
function initMarket() {
  const pos = (p) => p.value ? `<span class="pill pos ${p.value.replace(/ /g, "-")}">${esc(p.value)}</span>` : "";
  state.market = agGrid.createGrid($("marketGrid"), {
    theme: theme(), rowData: [], getRowId: (p) => p.data.sku, animateRows: true,
    defaultColDef: { sortable: true, resizable: true },
    overlayNoRowsTemplate: NO_ROWS('No check yet - ask the copilot "Are our prices competitive?" or press Run check.'),
    columnDefs: [
      { field: "product", headerName: "Product", flex: 1.2, minWidth: 140 },
      { field: "our_price", headerName: "Ours", width: 70, type: "rightAligned", cellClass: "strong" },
      { field: "low", headerName: "Low", width: 66, type: "rightAligned", valueFormatter: (p) => p.value || "—" },
      { field: "median", headerName: "Median", width: 76, type: "rightAligned", valueFormatter: (p) => p.value || "—" },
      { field: "high", headerName: "High", width: 66, type: "rightAligned", valueFormatter: (p) => p.value || "—" },
      { field: "vs_median_pct", headerName: "vs median", width: 92, type: "rightAligned",
        cellClassRules: { neg: (p) => p.value < -10, posv: (p) => p.value > 10 },
        valueFormatter: (p) => (p.value == null ? "—" : `${p.value > 0 ? "+" : ""}${p.value}%`) },
      { field: "position", headerName: "Position", width: 128, cellRenderer: pos },
      { field: "offers", headerName: "Offers", width: 70, type: "rightAligned" },
      { colId: "best", headerName: "Cheapest comparable", flex: 1.4, minWidth: 170, sortable: false,
        valueGetter: (p) => (p.data.comparables[0] ? `${p.data.comparables[0].price} · ${p.data.comparables[0].domain}` : "—"),
        tooltipValueGetter: (p) => (p.data.comparables[0] ? p.data.comparables[0].title : "") },
    ],
  });
  $("marketRun").addEventListener("click", async () => { $("marketRun").disabled = true; try { setMarket(await api("/api/merchant/market/refresh", { method: "POST" })); } finally { $("marketRun").disabled = false; } });
  loadMarket();
}
function setMarket(m) {
  state.market.setGridOption("rowData", m.rows);
  const n = $("marketNote");
  n.textContent = m.live ? "Live from Channel3 product search · in-stock, new offers in USD"
    : "Channel3 sample data (no CHANNEL3_API_KEY set) · same response schema · fictitious retailers";
  n.className = `note ${m.live ? "live" : "sample"}`;
}
async function loadMarket() { setMarket(await api("/api/merchant/market")); }

// ------------------------------------------------------------------ tabs
const HINTS = {
  orders: "live from the ledger · AG Grid",
  inventory: "stock from the ledger · AG Grid + AG Charts",
  fulfillment: "workshop plan · Bryntum Scheduler",
  market: "market check · Channel3",
};
function showTab(name) {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("on", t.dataset.tab === name));
  document.querySelectorAll(".pane").forEach((p) => { p.hidden = p.dataset.pane !== name; });
  $("tabHint").textContent = HINTS[name];
  state.tab = name;
  if (name === "fulfillment") { initBoard(); refreshBoard(); }
  if (name === "inventory") refreshInventory();
}
function onMerchantCard(card) {
  if (!card) return;
  if (card.type === "grid_view") applyGridView(card);
  if (card.type === "board") showTab("fulfillment");
  if (card.type === "market") { showTab("market"); loadMarket(); }
}

let lastKpi = {};
function setKpi(id, v) {
  const el = $(id);
  if (lastKpi[id] !== undefined && lastKpi[id] !== v) { el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }
  lastKpi[id] = v;
  el.textContent = v;
}

let refreshing = false;
async function refreshMerchant() {
  if (refreshing) return;
  refreshing = true;
  try { await refreshMerchantInner(); } finally { refreshing = false; }
}
async function refreshMerchantInner() {
  const [{ orders }, { events }] = await Promise.all([
    api("/api/merchant/orders"), api(`/api/merchant/events?after=${state.lastEvent}`),
  ]);
  if (state.grid) state.grid.setGridOption("rowData", orders);
  const sum = (arr, f) => arr.reduce((a, o) => a + parseFloat(o[f] || 0), 0).toFixed(2);
  const paid = orders.filter((o) => o.status === "PAID");
  const settled = orders.filter((o) => o.status === "PAID" || o.status === "REFUNDED");
  setKpi("kGross", sum(paid, "total"));
  setKpi("kFees", sum(settled, "paypal_fee"));
  setKpi("kNet", sum(paid, "net_amount"));
  setKpi("kPending", String(orders.filter((o) => ["AWAITING_PAYMENT", "APPROVED", "CAPTURED"].includes(o.status)).length));
  if (state.tab === "inventory") refreshInventory();
  if (events.some((e) => e.kind.startsWith("fulfillment."))) refreshBoard();
  const tl = $("timeline");
  for (const e of events) {
    if (e.kind === "agent.tool" && e.ok) { state.lastEvent = e.id; continue; }
    const li = document.createElement("li");
    const good = ["webhook.verified", "reconciled", "refund.confirmed"].includes(e.kind);
    li.className = `new ${e.ok ? (good ? "good" : "") : "bad"}`;
    li.innerHTML = `<span class="t">${hhmm(e.created_at)}</span><span><span class="k">${esc(e.kind)}</span>${e.order_id ? ` <span class="t">${esc(e.order_id)}</span>` : ""}<br><span class="d">${esc(e.detail)}</span></span>`;
    tl.appendChild(li);
    tl.scrollTop = tl.scrollHeight;
    state.lastEvent = e.id;
    if (e.kind === "webhook.rejected") {
      const v = parseInt($("kRejected").textContent || "0", 10) + 1;
      setKpi("kRejected", String(v));
      $("kRejected").classList.add("hot");
    }
  }
}

// ------------------------------------------------------------------ boot
async function newCustomerChat() {
  const r = await api("/api/conversations", { method: "POST", body: JSON.stringify({ kind: "customer", customer_name: "Lucía (Madrid)" }) });
  state.cust = r.conversation_id; state.lastCust = 0; $("custThread").innerHTML = "";
  storageSet("tendero.cust", state.cust);
}

async function boot() {
  state.cfg = await api("/api/config");
  $("storeName").textContent = state.cfg.store.name;
  const llm = $("llmBadge");
  llm.textContent = state.cfg.llm.demo ? "LLM: offline demo (no API key)" : `LLM: ${state.cfg.llm.label}`;
  llm.classList.add(state.cfg.llm.demo ? "demo" : "live");
  const pp = $("ppBadge");
  pp.textContent = state.cfg.paypal.mode === "sandbox" ? "PayPal sandbox" : "PayPal: local sandbox simulator";
  pp.classList.add(state.cfg.paypal.mode === "sandbox" ? "live" : "demo");
  if (!state.cfg.demo_tools) $("forgeBtn").remove();
  const sdk = $("sdkBadge");
  sdk.textContent = state.cfg.integrations.paypal_sdk.includes("APIMatic") ? "PayPal Server SDK · APIMatic" : "PayPal REST client";
  sdk.classList.add("live");

  initGrid();
  initInventory();
  initMarket();
  document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => showTab(t.dataset.tab)));
  state.cust = storageGet("tendero.cust");
  if (state.cust) { try { await api(`/api/conversations/${state.cust}/messages`); } catch { state.cust = null; } }
  if (!state.cust) await newCustomerChat();
  const m = await api("/api/conversations", { method: "POST", body: JSON.stringify({ kind: "merchant" }) });
  state.merch = m.conversation_id;

  $("custForm").addEventListener("submit", (e) => { e.preventDefault(); const v = $("custInput").value; $("custInput").value = ""; send("cust", v); });
  $("mForm").addEventListener("submit", (e) => { e.preventDefault(); const v = $("mInput").value; $("mInput").value = ""; send("merch", v); });
  $("closeCheckout").addEventListener("click", closeCheckout);
  $("resetBtn").addEventListener("click", newCustomerChat);
  $("sweepBtn").addEventListener("click", async () => { await api("/api/merchant/sweep", { method: "POST" }); refreshMerchant(); });
  $("forgeBtn").addEventListener("click", async () => { await api("/api/demo/forge-webhook", { method: "POST", body: "{}" }); refreshMerchant(); });

  await pullMessages("cust");
  await refreshMerchant();
  setInterval(() => { pullMessages("cust"); pullMessages("merch"); refreshMerchant(); }, 1000);
}
boot();
