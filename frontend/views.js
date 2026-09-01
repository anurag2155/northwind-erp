// Pure rendering: render(state) -> HTML string. No DOM, no fetch, no globals,
// which is what makes the console verifiable headlessly.
import { can } from "./api.js";

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const money = (c) => (Number(c || 0) / 100).toFixed(2);
const num = (n) => (Number.isInteger(Number(n)) ? String(Number(n)) : Number(n).toFixed(2));
const cls = (bad) => (bad ? "out" : "ok");

export const TABS = [
  { id: "warehouse", label: "Warehouse", need: ["GET", "/inventory/positions"] },
  { id: "procurement", label: "Procurement", need: ["GET", "/purchase-orders"] },
  { id: "sales", label: "Sales", need: ["GET", "/sales-orders"] },
  { id: "finance", label: "Finance", need: ["GET", "/ledger/inventory-reconciliation"] }];
export const visibleTabs = (s) => TABS.filter((t) => can(s.session, ...t.need));

const table = (head, rows) => rows.length
  ? `<table><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>` +
    rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") + `</tbody></table>`
  : "";

export const errorBox = (e) => {
  const p = Object.entries(e.detail || {}).filter(([k]) => k !== "error")
    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`);
  return `<p class="state error" role="alert"><strong>${esc(e.code)}</strong>` +
    `${e.status ? ` (HTTP ${e.status})` : ""}${p.length ? ` — ${esc(p.join(", "))}` : ""}</p>`;
};
const forbid = (w) => `<p class="state forbidden">Your role cannot view ${esc(w)}.</p>`;
const sect = (t, body, extra = "") => `<section><h2>${esc(t)}${extra}</h2>${body}</section>`;
const hint = (t) => ` <span class="hint">${esc(t)}</span>`;

/** Exactly one of: forbidden, error, loading, empty, data. */
function panel(s, key, what, allowed, draw) {
  if (!allowed) return forbid(what);
  if (s.errors[key]) return errorBox(s.errors[key]);
  if (s.loading[key]) return `<p class="state loading">Loading ${esc(what)}…</p>`;
  const d = s.data[key];
  if (!d || (Array.isArray(d) && !d.length)) return `<p class="state empty">No ${esc(what)} yet.</p>`;
  return draw(d);
}

const qtyIn = () => `<input name="qty" type="number" step="any" min="0.01" placeholder="Qty" required>`;
/** select-a-line + qty + submit. */
const lineForm = (klass, attr, id, field, opts, label) =>
  `<form class="${klass}" data-${attr}="${esc(id)}"><select name="${field}">${opts}</select>` +
  qtyIn() + `<button type="submit">${label}</button></form>`;

const tablePanel = (s, key, what, path, head, row) =>
  panel(s, key, what, can(s.session, "GET", path), (rows) => table(head, rows.map(row)));
const cardPanel = (s, key, what, path, draw) =>
  panel(s, key, what, can(s.session, "GET", path), (rows) => rows.map(draw).join(""));

const card = (id, badge, meta, body) =>
  `<article class="card"><header><code>${esc(id)}</code>` +
  `<span class="badge ${esc(badge)}">${esc(badge)}</span>${hint(meta)}</header>${body}</article>`;

export function renderWarehouse(s) {
  const stock = tablePanel(s, "positions", "stock positions", "/inventory/positions",
    ["SKU", "WH", "On hand", "Reserved", "Available", "Unit cost", "Value"],
    (p) => [esc(p.sku), esc(p.warehouse_id), num(p.on_hand), num(p.reserved),
      `<span class="${cls(p.available <= 0)}">${num(p.available)}</span>`,
      money(p.unit_cost_cents), money(p.value_cents)]);
  const moves = tablePanel(s, "movements", "stock movements", "/inventory/movements",
    ["#", "SKU", "WH", "Kind", "Qty", "Value", "Source", "By", "At"],
    (m) => [m.seq, esc(m.sku), esc(m.warehouse_id), esc(m.kind),
      `<span class="${cls(m.qty < 0)}">${num(m.qty)}</span>`, money(m.value_cents),
      esc(m.source), esc(m.posted_by), esc(m.posted_at)]);
  return sect("Stock positions", stock) +
    sect("Movement history", moves, hint("append-only, newest first"));
}

export function renderProcurement(s) {
  const list = cardPanel(s, "purchase_orders", "purchase orders", "/purchase-orders", (o) => {
      const lines = table(["Line", "SKU", "Ordered", "Received", "Outstanding", "Unit cost"],
        o.lines.map((l) => [esc(l.id.slice(0, 6)), esc(l.sku), num(l.qty_ordered), num(l.qty_received),
          `<span class="${cls(l.qty_outstanding > 0)}">${num(l.qty_outstanding)}</span>`,
          money(l.unit_cost_cents)]));
      let act = "";
      if (o.status === "submitted" && can(s.session, "POST", "/purchase-orders/{po_id}/approve"))
        act = `<button data-act="approve" data-po="${esc(o.id)}">Approve</button>`;
      else if (o.status === "approved" && can(s.session, "POST", "/purchase-orders/{po_id}/receipts"))
        act = lineForm("gr-form", "po", o.id, "po_line_id",
          o.lines.filter((l) => l.qty_outstanding > 0).map((l) => `<option value="${esc(l.id)}">` +
            `${esc(l.sku)} · ${num(l.qty_outstanding)} outstanding</option>`).join(""),
          "Record receipt");
      return card(o.id, o.status, `${money(o.amount_cents)} · tol ${num(o.over_tolerance)}` +
        ` · by ${o.created_by}${o.approved_by ? ` · approved ${o.approved_by}` : ""}`, lines + act);
  });
  const create = can(s.session, "POST", "/purchase-orders")
    ? sect("Raise a purchase order", `<form id="po-form">` +
        `<input name="sku" placeholder="SKU" value="WIDGET" required>` + qtyIn() +
        `<input name="unit_cost_cents" type="number" min="0" placeholder="Unit cost (cents)" required>` +
        `<button type="submit">Create</button></form>`) : "";
  return create + sect("Purchase orders", list);
}

export function renderSales(s) {
  const a = s.availability;
  const live = !a ? `<p class="state empty">Enter a SKU to see live availability.</p>`
    : a.error ? errorBox(a.error)
    : `<p class="live"><strong>${esc(a.sku)}</strong> @ ${esc(a.warehouse_id)}: on hand ` +
      `${num(a.on_hand)}, reserved ${num(a.reserved)}, <strong class="${cls(a.available <= 0)}">` +
      `available ${num(a.available)}</strong>${hint("re-read " + a.checked_at)}</p>`;
  const create = can(s.session, "POST", "/sales-orders")
    ? sect("Raise a sales order", `<form id="so-form">` +
        `<input name="sku" placeholder="SKU" value="WIDGET" required>` + qtyIn() +
        `<input name="unit_price_cents" type="number" min="0" placeholder="Unit price (cents)" required>` +
        `<label><input type="checkbox" name="allow_partial"> allow partial</label>` +
        `<button type="submit">Create + confirm</button></form>${live}`,
      hint("re-read from the server, never cached")) : "";
  const list = cardPanel(s, "sales_orders", "sales orders", "/sales-orders", (o) => {
      const lines = table(["Line", "SKU", "Ordered", "Reserved", "Shipped", "Backordered"],
        o.lines.map((l) => [esc(l.id.slice(0, 6)), esc(l.sku), num(l.qty_ordered), num(l.qty_reserved),
          num(l.qty_shipped),
          `<span class="${cls(l.qty_backordered > 0)}">${num(l.qty_backordered)}</span>`]));
      const ready = o.lines.filter((l) => l.qty_reserved - l.qty_shipped > 0);
      const act = ready.length && can(s.session, "POST", "/sales-orders/{so_id}/shipments")
        ? lineForm("ship-form", "so", o.id, "so_line_id", ready.map((l) =>
            `<option value="${esc(l.id)}">${esc(l.sku)} · ` +
            `${num(l.qty_reserved - l.qty_shipped)} ready</option>`).join(""), "Ship") : "";
      return card(o.id, o.status, `by ${o.created_by} · ${o.warehouse_id}`, lines + act);
  });
  return create + sect("Sales orders", list);
}

const flag = (v) => `<span class="${v ? "ok" : "zero"}">${v ? "PASS" : "FAIL"}</span>`;

export function renderFinance(s) {
  const r = s.data.recon;
  let ind;
  if (!can(s.session, "GET", "/ledger/inventory-reconciliation")) ind = forbid("the ledger");
  else if (s.errors.recon) ind = errorBox(s.errors.recon);
  else if (s.loading.recon || !r) ind = `<p class="state loading">Loading reconciliation…</p>`;
  else {
    const i1 = r.I1_gl_vs_movements, i2 = r.I2_position_vs_movements;
    const i3 = r.I3_entries_balanced, i4 = r.I4_po_received_vs_movements;
    // Every invariant the endpoint reports gets a row. The table is built from
    // the four the API is known to return rather than from Object.keys, so a
    // renamed key shows up as a missing row in the console evidence run instead
    // of silently vanishing -- but a NEW invariant must be added here too, and
    // I4 was added in exactly this pass after the backend gained it.
    ind = `<p class="recon ${r.ok ? "ok" : "bad"}"><strong>Ledger balance matches inventory ` +
      `movements: ${r.ok ? "YES" : "NO"}</strong></p>` + table(["Invariant", "Result", "Detail"], [
        ["I1 GL 1300 vs movement value", flag(i1.ok), `${money(i1.gl_inventory_cents)} vs ` +
          `${money(i1.movement_value_cents)}, delta ${money(i1.delta_cents)}`],
        ["I2 position vs movements", flag(i2.ok), `${i2.drifted.length} drifted`],
        ["I3 entries balanced", flag(i3.ok), `${i3.unbalanced_entries.length} unbalanced`],
        ["I4 PO received vs movements", flag(i4.ok),
          `${i4.miscounted_lines.length} miscounted lines`]]);
  }
  const ledger = cardPanel(s, "journals", "journal entries", "/ledger/journals", (e) =>
    card(e.id, e.source, `${money(e.total_cents)} · ${e.posted_by} · ${e.posted_at}` +
      `${e.reverses_id ? ` · reverses ${e.reverses_id}` : ""}`, e.lines.length
        ? table(["Account", "Debit", "Credit"], e.lines.map((l) => [esc(l.account),
            l.debit_cents ? money(l.debit_cents) : "", l.credit_cents ? money(l.credit_cents) : ""]))
        : `<p class="state empty">No lines — a zero-value event (e.g. a free receipt).</p>`));
  return sect("Reconciliation", ind) + sect("Ledger", ledger);
}

export const renderNav = (s) =>
  `<nav><span class="who">${esc(s.session.id)} · <b>${esc(s.session.role)}</b>` +
  hint(`${s.session.permissions.length} permitted routes`) + `</span>` +
  visibleTabs(s).map((t) => `<button data-tab="${t.id}"` +
    `${t.id === s.tab ? ' class="active"' : ""}>${esc(t.label)}</button>`).join("") +
  `<button data-act="refresh">Refresh</button><button data-act="logout">Sign out</button></nav>`;

export function renderApp(s) {
  if (!s.session) return `<main class="signin">` +
    (s.errors.session ? errorBox(s.errors.session) : "") +
    `<form id="login"><h1>Northwind Operations</h1>` +
    hint("Paste a bearer token printed by python3 server.py") +
    `<input name="token" placeholder="bearer token" required>` +
    `<button type="submit">Sign in</button></form></main>`;
  const body = { warehouse: renderWarehouse, procurement: renderProcurement,
    sales: renderSales, finance: renderFinance }[s.tab];
  const open = visibleTabs(s).some((t) => t.id === s.tab);
  return renderNav(s) + `<main>${open ? body(s) : forbid("this area")}` +
    (s.notice ? (s.notice.code ? errorBox(s.notice) : `<p class="notice">${esc(s.notice)}</p>`) : "") +
    `</main>`;
}
