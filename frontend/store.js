// State and loading; no DOM, so evidence.mjs drives the same data path the
// browser uses.
import { ApiError, can } from "./api.js";

export function emptyState() {
  return {
    session: null, tab: "warehouse", notice: null, availability: null,
    data: { positions: [], movements: [], purchase_orders: [], sales_orders: [], journals: [], recon: null },
    loading: {}, errors: {},
  };
}

/** Failures become rendered state, never a swallowed exception. */
async function fetchInto(state, key, fn) {
  state.loading[key] = true;
  delete state.errors[key];
  try {
    state.data[key] = await fn();
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    state.errors[key] = err;
  } finally {
    state.loading[key] = false;
  }
}

export async function signIn(state, api) {
  delete state.errors.session;
  try {
    state.session = await api.get("/me");
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    state.session = null;
    state.errors.session = err;
  }
  return state;
}

const NEEDS = {
  warehouse: [["positions", "GET", "/inventory/positions"], ["movements", "GET", "/inventory/movements"]],
  procurement: [["purchase_orders", "GET", "/purchase-orders"]],
  sales: [["sales_orders", "GET", "/sales-orders"]],
  finance: [["recon", "GET", "/ledger/inventory-reconciliation"], ["journals", "GET", "/ledger/journals"]],
};

const CALL = {
  positions: (api) => api.get("/inventory/positions").then((r) => r.positions),
  movements: (api) => api.get("/inventory/movements?limit=25").then((r) => r.movements),
  purchase_orders: (api) => api.get("/purchase-orders").then((r) => r.purchase_orders),
  sales_orders: (api) => api.get("/sales-orders").then((r) => r.sales_orders),
  journals: (api) => api.get("/ledger/journals").then((r) => r.journals),
  recon: (api) => api.get("/ledger/inventory-reconciliation"),
};

/** Load only what the tab shows and the role may call. Every load re-reads the
 *  server: no cache to go stale, so a cold load and a refresh agree. */
export async function loadTab(state, api, tab = state.tab) {
  const needs = (NEEDS[tab] || []).filter(([, m, p]) => can(state.session, m, p));
  await Promise.all(needs.map(([key]) => fetchInto(state, key, () => CALL[key](api))));
  // A write may have moved the figure the Sales view is showing -- including the
  // caller's own reservation -- so re-read it here rather than waiting for a poll.
  const a = state.availability;
  if (a?.sku) await refreshAvailability(state, api, a.sku, a.warehouse_id);
  return state;
}

/** Read from the server every call, stamped with the read time; never derived
 *  from the positions list, which may be seconds old. */
export async function refreshAvailability(state, api, sku, warehouse_id) {
  if (!sku) { state.availability = null; return state; }
  try {
    const snap = await api.availability(sku, warehouse_id);
    state.availability = { ...snap, checked_at: new Date().toISOString().slice(11, 19) };
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    state.availability = { sku, warehouse_id, error: err };
  }
  return state;
}

/** Writes keep their failure as the ApiError itself, so the one formatter in
 *  views.js renders it; then the tab is re-read from the server. */
export async function act(state, api, fn, okMessage) {
  state.notice = null;
  try {
    const out = await fn();
    state.notice = typeof okMessage === "function" ? okMessage(out) : okMessage;
  } catch (err) {
    if (!(err instanceof ApiError)) throw err;
    state.notice = err;
  }
  await loadTab(state, api);
  return state;
}
