"""Regression suite for Stage 4. Run: python3 regression.py <erp.py> <readmodel.py>

Each scenario is run against BOTH the seeded build and the fixed build. A test
that only passes on the fixed build proves nothing on its own -- it has to fail
on the buggy one, or it is not testing the bug.
"""
import json, shutil, sys, threading, urllib.error, urllib.request
from collections import Counter
import os
def use(src, dst):
  if os.path.abspath(src) != os.path.abspath(dst): shutil.copy(src, dst)
use(sys.argv[1], 'erp.py'); use(sys.argv[2], 'readmodel.py')
import erp, server
T = server.reset_database(); httpd, PORT = server.serve(0)

def call(m, p, a, b=None):
  d = json.dumps(b).encode() if b is not None else None
  r = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, p), data=d, method=m,
      headers={"Authorization": "Bearer " + T[a], "Content-Type": "application/json"})
  try:
    with urllib.request.urlopen(r, timeout=30) as x: return x.status, json.loads(x.read())
  except urllib.error.HTTPError as e: return e.code, json.loads(e.read())

def stock(qty, cost=100, sku="WIDGET", wh="WH-MAIN"):
  po = call("POST", "/purchase-orders", "ravi",
            {"warehouse_id": wh, "lines": [{"sku": sku, "qty": qty, "unit_cost_cents": cost}]})[1]
  call("POST", "/purchase-orders/%s/approve" % po["id"], "meera", {})
  call("POST", "/purchase-orders/%s/receipts" % po["id"], "kabir",
       {"warehouse_id": wh, "lines": [{"po_line_id": po["lines"][0]["id"], "qty": qty}]})
  return po

def look(sku="WIDGET", wh="WH-MAIN"):
  return call("GET", "/inventory/skus/%s/availability?warehouse_id=%s" % (sku, wh), "nina")[1]

def rows(sql, *a):
  c = erp.connect()
  try: return [dict(r) for r in c.execute(sql, a)]
  finally: c.close()

out = []
def report(name, ok, detail):
  out.append((name, ok, detail))

# R1 -- reservation race: 1 unit, 8 concurrent confirms
stock(1, 250)
ids = [call("POST", "/sales-orders", "nina", {"warehouse_id": "WH-MAIN",
       "lines": [{"sku": "WIDGET", "qty": 1, "unit_price_cents": 900}]})[1]["id"] for _ in range(8)]
gate, res, lk = threading.Barrier(8), [], threading.Lock()
def go(i):
  gate.wait(); st, b = call("POST", "/sales-orders/%s/confirm" % i, "nina", {})
  with lk: res.append("200" if st == 200 else "%s %s" % (st, b.get("error", "")))
ts = [threading.Thread(target=go, args=(i,)) for i in ids]
[t.start() for t in ts]; [t.join() for t in ts]
s1 = look()
report("R1 reservation race", res.count("200") == 1 and s1["reserved"] <= s1["on_hand"],
       "%d winners for 1 unit, reserved=%s on_hand=%s, peak in-flight %d, codes=%s"
       % (res.count("200"), s1["reserved"], s1["on_hand"], server.IN_FLIGHT["peak"],
          dict(Counter(res))))

# R2 -- two partial receipts against ONE po line
po = call("POST", "/purchase-orders", "ravi", {"warehouse_id": "WH-MAIN",
     "lines": [{"sku": "GIZMO", "qty": 10, "unit_cost_cents": 100}]})[1]
call("POST", "/purchase-orders/%s/approve" % po["id"], "meera", {})
line = po["lines"][0]["id"]
def recv(q):
  return call("POST", "/purchase-orders/%s/receipts" % po["id"], "kabir",
              {"warehouse_id": "WH-MAIN", "lines": [{"po_line_id": line, "qty": q}]})
r1, r2 = recv(6), recv(4)
db = rows("SELECT qty_received FROM po_lines WHERE id=?", line)[0]["qty_received"]
third = recv(1)
report("R2 partial receipt", r1[0] == 201 and r2[0] == 201 and float(db) == 10.0
       and third[0] == 409,
       "6 then 4 -> qty_received=%s (want 10), outstanding=%s, a further +1 -> %s %s"
       % (db, r2[1]["lines"][0]["qty_outstanding"] if r2[0] == 201 else "n/a",
          third[0], third[1].get("error", "")))

# R3 -- finance ledger must be accountant-only
clerk = call("GET", "/ledger/journals", "kabir")
acct = call("GET", "/ledger/journals", "tara")
recon_clerk = call("GET", "/ledger/inventory-reconciliation", "kabir")
report("R3 finance authz", clerk[0] == 403 and acct[0] == 200 and recon_clerk[0] == 403,
       "warehouse_clerk /ledger/journals -> %d, /ledger/inventory-reconciliation -> %d, "
       "accountant -> %d" % (clerk[0], recon_clerk[0], acct[0]))

# R4 -- transfer is a linked pair of movements, not a balance edit
before = len(rows("SELECT id FROM stock_movements"))
# GIZMO has 10 on hand from R2 and nothing reserved against it.
tr = call("POST", "/inventory/transfers", "kabir",
          {"sku": "GIZMO", "from_warehouse_id": "WH-MAIN", "to_warehouse_id": "WH-WEST", "qty": 4})
made = rows("SELECT sku, warehouse_id, kind, qty, value_cents, journal_id FROM stock_movements "
            "ORDER BY rowid DESC LIMIT 2")
paired = len(made) == 2 and made[0]["journal_id"] == made[1]["journal_id"] \
    and {m["kind"] for m in made} == {"issue", "receipt"} \
    and made[0]["qty"] == -made[1]["qty"] and made[0]["value_cents"] == -made[1]["value_cents"]
after = len(rows("SELECT id FROM stock_movements"))
src, dst = look("GIZMO", "WH-MAIN"), look("GIZMO", "WH-WEST")
rec = call("GET", "/ledger/inventory-reconciliation", "tara")[1]
report("R4 transfer", tr[0] == 201 and paired and after == before + 2
       and src["on_hand"] == 6.0 and dst["on_hand"] == 4.0 and rec["ok"],
       "status %s, +%d movements sharing journal %s, src on_hand=%s dst on_hand=%s, recon ok=%s"
       % (tr[0], after - before, (made[0]["journal_id"] or "")[:8], src["on_hand"],
          dst["on_hand"], rec["ok"]))

# transfers must not oversell reserved stock either
over = call("POST", "/inventory/transfers", "kabir",
            {"sku": "GIZMO", "from_warehouse_id": "WH-MAIN", "to_warehouse_id": "WH-WEST",
             "qty": 99})
report("R4b transfer bounds", over[0] == 409,
       "transferring 99 with 6 available -> %s %s" % (over[0], over[1].get("error", "")))

# R4c -- the transfer's own boundaries: role, arguments, unknown references
def transfer(actor="kabir", **kw):
  b = {"sku": "GIZMO", "from_warehouse_id": "WH-MAIN", "to_warehouse_id": "WH-WEST", "qty": 1}
  b.update(kw)
  return call("POST", "/inventory/transfers", actor, b)
edges = {
  "sales_rep denied": (transfer(actor="nina")[0], 403),
  "accountant denied": (transfer(actor="tara")[0], 403),
  "same warehouse": (transfer(to_warehouse_id="WH-MAIN")[0], 400),
  "unknown warehouse": (transfer(to_warehouse_id="WH-NOPE")[0], 404),
  "unknown sku": (transfer(sku="NOSUCH")[0], 404),
  "negative qty": (transfer(qty=-5)[0], 400),
}
report("R4c transfer edges", all(got == want for got, want in edges.values()),
       ", ".join("%s->%d" % (k, v[0]) for k, v in edges.items()))

# R4d -- a full round trip conserves stock and leaves the ledger untouched
gl_before = rows("SELECT COALESCE(SUM(debit_cents-credit_cents),0) b FROM journal_lines "
                 "WHERE account='1300'")[0]["b"]
call("POST", "/inventory/transfers", "kabir", {"sku": "GIZMO", "from_warehouse_id": "WH-MAIN",
     "to_warehouse_id": "WH-WEST", "qty": 6})
back = call("POST", "/inventory/transfers", "kabir", {"sku": "GIZMO",
            "from_warehouse_id": "WH-WEST", "to_warehouse_id": "WH-MAIN", "qty": 10})
gl_after = rows("SELECT COALESCE(SUM(debit_cents-credit_cents),0) b FROM journal_lines "
                "WHERE account='1300'")[0]["b"]
rt_src, rt_dst = look("GIZMO", "WH-MAIN"), look("GIZMO", "WH-WEST")
rec2 = call("GET", "/ledger/inventory-reconciliation", "tara")[1]
report("R4d round trip", back[0] == 201 and rt_src["on_hand"] == 10.0
       and rt_dst["on_hand"] == 0.0 and gl_before == gl_after and rec2["ok"],
       "out 6 then back 10 -> src=%s dst=%s, GL 1300 unchanged at %sc, recon ok=%s"
       % (rt_src["on_hand"], rt_dst["on_hand"], gl_after, rec2["ok"]))

label = sys.argv[3] if len(sys.argv) > 3 else sys.argv[1]
print("=== %s" % label)
for n, ok, d in out:
  print("  %-22s %s  %s" % (n, "PASS" if ok else "FAIL", d))
httpd.shutdown()
sys.exit(0 if all(o for _, o, _ in out) else 1)
