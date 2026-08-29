// The only browser-coupled file: DOM events in, store calls out.
import { createApi } from "./api.js";
import { emptyState, signIn, loadTab, refreshAvailability, act } from "./store.js";
import { renderApp } from "./views.js";

// Where the API lives, most specific first: a ?api= override for sharing a link,
// then whatever this browser last used, then the value baked into config.js at
// deploy time, then the local default. No rebuild needed to repoint the console.
const query = typeof location === "undefined" ? "" : location.search;
const BASE = new URLSearchParams(query).get("api") || localStorage.getItem("erp.base")
  || (typeof window === "undefined" ? null : window.ERP_BASE) || "http://127.0.0.1:8080";
localStorage.setItem("erp.base", BASE);
const root = document.getElementById("root");
let state = emptyState();
let api = null;
let timer = null;

const paint = () => { root.innerHTML = renderApp(state); };

async function reload() {
  paint();                       // paint the loading state first
  await loadTab(state, api);
  paint();
}

// A repaint is always truthful but clobbers half-typed input, so skip it while
// a field has focus.
const busy = () => ["INPUT", "SELECT"].includes(document.activeElement?.tagName);

function startPolling() {
  clearInterval(timer);
  timer = setInterval(async () => {
    if (document.hidden || !api) return;
    const sku = document.getElementById("so-form")?.sku.value.trim();
    await (sku ? refreshAvailability(state, api, sku, "WH-MAIN") : loadTab(state, api));
    if (!busy()) paint();
  }, 3000);
}

async function start(token) {
  api = createApi(BASE, token);
  await signIn(state, api);
  if (!state.session) return paint();
  localStorage.setItem("erp.token", token);
  await reload();
  startPolling();
}

const finish = async (p) => { await p; paint(); };
const val = (f, name) => f[name]?.value?.trim();

root.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = ev.target;
  if (f.id === "login") return start(val(f, "token"));
  if (f.id === "po-form") {
    return finish(act(state, api, () => api.post("/purchase-orders", {
      warehouse_id: "WH-MAIN",
      lines: [{ sku: val(f, "sku"), qty: Number(val(f, "qty")),
                unit_cost_cents: Number(val(f, "unit_cost_cents")) }],
    }), (po) => `Purchase order ${po.id} created (${po.status}).`));
  }
  if (f.classList.contains("gr-form")) {
    return finish(act(state, api, () => api.post(`/purchase-orders/${f.dataset.po}/receipts`, {
      warehouse_id: "WH-MAIN",
      lines: [{ po_line_id: val(f, "po_line_id"), qty: Number(val(f, "qty")) }],
    }), (gr) => `Receipt posted, journal ${gr.journal_id}.`));
  }
  if (f.id === "so-form") {
    // Re-read before confirming: the screen may be a poll old.
    await refreshAvailability(state, api, val(f, "sku"), "WH-MAIN");
    return finish(act(state, api, async () => {
      const so = await api.post("/sales-orders", {
        warehouse_id: "WH-MAIN",
        lines: [{ sku: val(f, "sku"), qty: Number(val(f, "qty")),
                  unit_price_cents: Number(val(f, "unit_price_cents")) }],
      });
      return api.post(`/sales-orders/${so.id}/confirm`, { allow_partial: f.allow_partial.checked });
    }, (so) => `Sales order ${so.id} ${so.status}.`));
  }
  if (f.classList.contains("ship-form")) {
    return finish(act(state, api, () => api.post(`/sales-orders/${f.dataset.so}/shipments`, {
      lines: [{ so_line_id: val(f, "so_line_id"), qty: Number(val(f, "qty")) }],
    }), (sh) => `Shipment posted, order now ${sh.status}.`));
  }
});

root.addEventListener("click", (ev) => {
  const b = ev.target.closest("button");
  if (!b) return;
  if (b.dataset.tab) { state.tab = b.dataset.tab; state.notice = null; return reload(); }
  if (b.dataset.act === "refresh") return reload();
  if (b.dataset.act === "approve") {
    return finish(act(state, api, () => api.post(`/purchase-orders/${b.dataset.po}/approve`),
      "Purchase order approved."));
  }
  if (b.dataset.act === "logout") {
    clearInterval(timer);
    localStorage.removeItem("erp.token");
    state = emptyState(); api = null; paint();
  }
});

let debounce = null;
root.addEventListener("input", (ev) => {
  if (ev.target.form?.id !== "so-form" || ev.target.name !== "sku") return;
  const sku = ev.target.value.trim();
  clearTimeout(debounce);
  debounce = setTimeout(async () => {
    await refreshAvailability(state, api, sku, "WH-MAIN");
    if (!busy()) paint();
  }, 250);
});

paint();
const saved = localStorage.getItem("erp.token");
if (saved) start(saved);
