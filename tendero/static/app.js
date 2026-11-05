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
function initGrid() {
  const money = (p) => (p.value == null ? "—" : p.value);
  const cols = [
    { field: "id", headerName: "Order", width: 96, cellClass: "mono" },
    { field: "customer_name", headerName: "Customer", flex: 1, minWidth: 110 },
    { field: "status", headerName: "Status", width: 150,
      cellRenderer: (p) => `<span class="pill ${p.value}">${p.value.replace("_", " ")}</span>`,
      comparator: (a, b) => STATUS_ORDER.indexOf(a) - STATUS_ORDER.indexOf(b) },
    { field: "total", headerName: "Total", width: 86, type: "rightAligned" },
    { field: "paypal_fee", headerName: "Fee", width: 76, type: "rightAligned", valueFormatter: money },
    { field: "net_amount", headerName: "Net", width: 82, type: "rightAligned", valueFormatter: money },
    { field: "paid_via", headerName: "Confirmed by", flex: 1, minWidth: 130, valueFormatter: (p) => p.value || "—" },
    { field: "paypal_order_id", headerName: "PayPal order", width: 170, valueFormatter: (p) => p.value || "—" },
  ];
  state.grid = agGrid.createGrid($("ordersGrid"), {
    columnDefs: cols, rowData: [], getRowId: (p) => p.data.id, animateRows: true,
    defaultColDef: { sortable: true, resizable: true },
    overlayNoRowsTemplate: '<span style="color:#8a93a1">No orders yet - start chatting on the left.</span>',
  });
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

  initGrid();
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
