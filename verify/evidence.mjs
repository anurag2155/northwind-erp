// Headless console evidence. Run from this directory: node evidence.mjs
// Boots the real backend and drives the console's own modules -- the same ones
// app.js mounts -- so what is printed is what the browser paints.
import { spawn } from "node:child_process";
import { createApi, can } from "../frontend/api.js";
import { emptyState, signIn, loadTab, refreshAvailability, act } from "../frontend/store.js";
import { renderApp, renderWarehouse, renderProcurement, renderSales, renderFinance, visibleTabs,
  errorBox } from "../frontend/views.js";

const BOOT = `
import json, threading, server
tokens = server.reset_database()
httpd, port = server.serve(0)
print(json.dumps({"port": port, "tokens": tokens}), flush=True)
threading.Event().wait()
`;

/** Rendered HTML with tags stripped: what the browser gets, made readable. */
const text = (html) => html
  .replace(/<(script|style)[\s\S]*?<\/\1>/g, "")
  .replace(/<\/(tr|article|section|p|form|table)>/g, "\n")
  .replace(/<\/(th|td)>/g, " | ")
  .replace(/<\/(code|span|h2|header|strong|b|button|option)>/g, "$& ")
  .replace(/<[^>]+>/g, "")
  .replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&").replace(/&#39;/g, "'")
  .split("\n").map((l) => l.replace(/[ \t]+/g, " ").trim()).filter(Boolean).join("\n");

let failures = 0;
function check(label, cond, evidence) {
  if (!cond) { failures++; console.log(`FAIL ${label} ${JSON.stringify(evidence ?? null)}`); }
}
const show = (title, body) => console.log(`\n--- ${title}\n${body}`);

async function boot() {
  const proc = spawn("python3", ["-c", BOOT], { cwd: "../backend", stdio: ["ignore", "pipe", "inherit"] });
  const line = await new Promise((res, rej) => {
    let buf = "";
    proc.stdout.on("data", (d) => { buf += d; if (buf.includes("\n")) res(buf.split("\n")[0]); });
    proc.on("exit", (c) => rej(new Error(`backend exited ${c}`)));
  });
  return { proc, ...JSON.parse(line) };
}

/** One session: its own api client and state, as a second tab would have. */
async function session(base, token, tab = "warehouse") {
  const api = createApi(base, token);
  const state = emptyState();
  state.tab = tab;
  await signIn(state, api);
  return { api, state };
}

const main = async () => {
  const { proc, port, tokens } = await boot();
  const base = `http://127.0.0.1:${port}`;
  console.log("CONSOLE EVIDENCE — backend " + base);

  // E1 RBAC
  const [clerk, rep, acct] = await Promise.all([
    session(base, tokens.kabir, "procurement"), session(base, tokens.nina, "procurement"),
    session(base, tokens.tara, "finance")]);
  const tabs = (x) => visibleTabs(x.state).map((t) => t.id);
  check("e1.tabs_differ", String(tabs(clerk)) !== String(tabs(rep)), [tabs(clerk), tabs(rep)]);
  check("e1.finance_is_accountant_only",
    tabs(acct).includes("finance") && !tabs(clerk).includes("finance"), [tabs(clerk), tabs(acct)]);
  console.log(`\nE1 RBAC SURFACE (tabs come from GET /me, not the frontend)`);
  for (const [who, s] of [["kabir/warehouse_clerk", clerk], ["nina/sales_rep", rep],
                          ["tara/accountant", acct]])
    console.log(`  ${who.padEnd(24)} tabs=[${visibleTabs(s.state).map((t) => t.id).join(", ")}]` +
      `  routes=${s.state.session.permissions.length}`);

  // A role that may not read purchase orders gets the forbidden state.
  await loadTab(rep.state, rep.api, "procurement");
  const repProc = text(renderProcurement(rep.state));
  check("e1.forbidden_rendered", /cannot view purchase orders/i.test(repProc), repProc);
  console.log(`  sales_rep on Procurement: ${repProc}`);

  // E2 a real buy -> approve -> receive flow
  const buyer = await session(base, tokens.ravi, "procurement");
  const mgr = await session(base, tokens.meera, "procurement");
  await act(buyer.state, buyer.api, () => buyer.api.post("/purchase-orders", {
    warehouse_id: "WH-MAIN",
    lines: [{ sku: "WIDGET", qty: 10, unit_cost_cents: 250 }] }), "created");
  await loadTab(buyer.state, buyer.api, "procurement");
  const po = buyer.state.data.purchase_orders[0];
  await act(mgr.state, mgr.api, () => mgr.api.post(`/purchase-orders/${po.id}/approve`), "ok");
  await act(clerk.state, clerk.api, () => clerk.api.post(`/purchase-orders/${po.id}/receipts`, {
    warehouse_id: "WH-MAIN", lines: [{ po_line_id: po.lines[0].id, qty: 6 }] }), "ok");
  await loadTab(clerk.state, clerk.api, "procurement");
  const proc6 = text(renderProcurement(clerk.state));
  check("e2.partial_outstanding", /10 \| 6 \| 4/.test(proc6), proc6);
  console.log(`\nE2 PARTIAL RECEIPT rendered as: ` +
    proc6.split("\n").find((l) => /WIDGET/.test(l)));

  // E3 live availability
  const seller = await session(base, tokens.nina, "sales");
  await refreshAvailability(seller.state, seller.api, "WIDGET", "WH-MAIN");
  const before = seller.state.availability.available;

  // A separate session reserves 4 units. Session A is not told; it must re-read.
  const other = await session(base, tokens.nina, "sales");
  const so = await other.api.post("/sales-orders", { warehouse_id: "WH-MAIN",
    lines: [{ sku: "WIDGET", qty: 4, unit_price_cents: 900 }] });
  await other.api.post(`/sales-orders/${so.id}/confirm`, {});

  const stale = seller.state.availability.available;      // still the cached render
  await refreshAvailability(seller.state, seller.api, "WIDGET", "WH-MAIN");
  const after = seller.state.availability.available;
  check("e3.dropped", before === 6 && stale === 6 && after === 2, { before, stale, after });
  console.log(`\nE3 LIVE AVAILABILITY: A reads ${before}; B (separate session) reserves 4; ` +
    `A's last paint still says ${stale} (a cache would stop here); A re-reads and shows ${after}.`);
  show("E3b Sales tab for A after the re-read (rendered)",
    text(renderSales(seller.state)).split("\n").filter((l) => /available/.test(l)).join("\n"));

  // Regression: after the caller's OWN reservation the box showed the pre-action
  // number until the next poll; loadTab now re-reads availability after a write.
  await act(seller.state, seller.api, async () => {
    const o = await seller.api.post("/sales-orders", { warehouse_id: "WH-MAIN",
      lines: [{ sku: "WIDGET", qty: 1, unit_price_cents: 900 }] });
    return seller.api.post(`/sales-orders/${o.id}/confirm`, {});
  }, "ok");
  const own = seller.state.availability.available;
  const server = (await seller.api.availability("WIDGET", "WH-MAIN")).available;
  check("e3c.own_action_not_stale", own === server, { own, server });
  console.log(`  after A's own reservation: box=${own}, server=${server} (was stale by one action)`);

  // E4 error is surfaced
  await act(seller.state, seller.api, async () => {
    const s = await seller.api.post("/sales-orders", { warehouse_id: "WH-MAIN",
      lines: [{ sku: "WIDGET", qty: 99, unit_price_cents: 900 }] });
    return seller.api.post(`/sales-orders/${s.id}/confirm`, {});
  }, "should not happen");
  check("e4.surfaced", seller.state.notice?.code === "INSUFFICIENT_STOCK", seller.state.notice);
  console.log(`\nE4 OVER-RESERVE SURFACED, NOT SWALLOWED\n  rendered: ` +
    text(errorBox(seller.state.notice)));

  // Backend down is its own state, not a blank screen.
  const deadState = emptyState();
  await signIn(deadState, createApi("http://127.0.0.1:1", tokens.nina));
  check("e4.backend_down", deadState.errors.session?.code === "BACKEND_UNREACHABLE");
  console.log(`  offline API: ${text(renderApp(deadState)).split("\n")[0]}`);

  // E5 reconciliation indicator
  await loadTab(acct.state, acct.api, "finance");
  const fin = text(renderFinance(acct.state));
  check("e5.indicator", /Ledger balance matches inventory movements: YES/.test(fin), fin.slice(0, 300));
  // Every invariant the API reports must reach the screen. Without this the
  // console could quietly drop one -- which is what happened when the backend
  // gained I4 and this view still listed three rows.
  const shown = ["I1", "I2", "I3", "I4"].filter((i) => fin.includes(`${i} `));
  check("e5.all_invariants_rendered", shown.length === 4, shown);
  show("E5 Finance (rendered)", fin.split("\n").slice(0, 6).join("\n"));

  // E6 a cold load reflects true state; E7 empty
  const fresh = await session(base, tokens.kabir, "warehouse");
  await loadTab(fresh.state, fresh.api, "warehouse");
  const wh = text(renderWarehouse(fresh.state));
  const truth = (await createApi(base, tokens.kabir).get("/inventory/positions")).positions[0];
  check("e6.matches_backend", wh.includes(`${truth.on_hand} | ${truth.reserved}`), truth);
  console.log(`\nE6 COLD SESSION matches the backend: positions row renders ` +
    `on_hand ${truth.on_hand}, reserved ${truth.reserved}`);

  fresh.state.data.movements = [];
  const emptied = text(renderWarehouse(fresh.state));
  check("e7.empty", /No stock movements yet/.test(emptied));
  console.log(`\nE7 EMPTY STATE  ${emptied.split("\n").pop()}`);

  // E8 app.js driven through its own handlers on a stub DOM, so the one
  // browser-coupled file is not an untested seam.
  const L = {}, el = { innerHTML: "", addEventListener: (t, f) => (L[t] = f) };
  Object.assign(globalThis, {
    document: { getElementById: () => el, querySelectorAll: () => [], activeElement: null },
    localStorage: { m: { "erp.base": base }, getItem(k) { return this.m[k] ?? null; },
      setItem(k, v) { this.m[k] = v; }, removeItem(k) { delete this.m[k]; } },
    setInterval: () => 0, clearInterval: () => {} });
  await import("../frontend/app.js");
  const F = (v) => ({ value: v });
  const send = (t) => L.submit({ preventDefault() {}, target: { classList: { contains: () => false }, ...t } });
  check("e8.signin_screen", /Sign in/.test(el.innerHTML));
  await send({ id: "login", token: F(tokens.ravi) });
  check("e8.role_correct_tabs", /data-tab="procurement"/.test(el.innerHTML)
    && !/data-tab="finance"/.test(el.innerHTML), el.innerHTML.slice(0, 90));
  await L.click({ target: { closest: () => ({ dataset: { tab: "procurement" } }) } });
  await send({ id: "po-form", sku: F("WIDGET"), qty: F("4"), unit_cost_cents: F("250") });
  check("e8.po_created", /Purchase order .* created/.test(el.innerHTML));
  await send({ id: "po-form", sku: F("NOPE"), qty: F("1"), unit_cost_cents: F("1") });
  check("e8.bad_write_surfaced", /state error/.test(el.innerHTML));
  console.log(`\nE8 APP.JS via its own handlers: sign-in, role-correct tabs, tab ` +
    `switch, PO created, rejected write surfaced`);

  proc.kill();
  console.log(failures ? `\n${failures} CHECK(S) FAILED` : "\nALL CONSOLE EVIDENCE CHECKS PASSED");
  process.exit(failures ? 1 : 0);
};

main().catch((e) => { console.error(e); process.exit(1); });
