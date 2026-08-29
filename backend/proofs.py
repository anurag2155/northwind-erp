#!/usr/bin/env python3
"""Proof suite. Exits non-zero on the first failed assertion, so ALL PROOFS
PASSED cannot come from a broken build; each proof rebuilds the DB.
"""
import json, sys, threading, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

import erp, server

TOKENS, PORT, _httpd = {}, 0, None

def call(method, path, actor, body=None):
  data = json.dumps(body).encode() if body is not None else None
  req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data,
                               method=method, headers={"Content-Type": "application/json",
                               "Authorization": "Bearer " + TOKENS[actor]})
  try:
    with urllib.request.urlopen(req, timeout=30) as res:
      return res.status, json.loads(res.read())
  except urllib.error.HTTPError as err:
    return err.code, json.loads(err.read())

def restart():
  global TOKENS, PORT, _httpd
  if _httpd is not None:
    _httpd.shutdown(); _httpd.server_close()
  TOKENS = server.reset_database()
  _httpd, PORT = server.serve()

def ok(label, cond, evidence=None):
  if not cond:
    print("FAIL %s %s" % (label, json.dumps(evidence, default=str)))
    sys.exit(1)

def sql(statement, *params):    # direct DB access, bypassing the API
  conn = erp.connect()
  try:
    conn.execute(statement, params)
    return None
  except Exception as err:
    return type(err).__name__
  finally:
    conn.close()

def po(qty, cost, tol=0, sku="WIDGET"):
  s, b = call("POST", "/purchase-orders", "ravi",
              {"warehouse_id": "WH-MAIN", "over_tolerance": tol,
               "lines": [{"sku": sku, "qty": qty, "unit_cost_cents": cost}]})
  ok("po", s == 201, b)
  return b

def approve(order, actor="meera"):
  return call("POST", "/purchase-orders/%s/approve" % order["id"], actor, {})

def receive(order, *qtys, line=0):       # receive(o, 6, 6) names the line twice
  return call("POST", "/purchase-orders/%s/receipts" % order["id"], "kabir",
              {"warehouse_id": "WH-MAIN",
               "lines": [{"po_line_id": order["lines"][line]["id"], "qty": q} for q in qtys]})

def stock(qty, cost, sku="WIDGET"):      # buy into WH-MAIN so we have stock
  order = po(qty, cost, sku=sku)
  ok("stock.approve", approve(order)[0] == 200)
  ok("stock.receive", receive(order, qty)[0] == 201)
  return order

def so(*lines):
  s, b = call("POST", "/sales-orders", "nina",
              {"warehouse_id": "WH-MAIN",
               "lines": [{"sku": k, "qty": q, "unit_price_cents": p} for k, q, p in lines]})
  ok("so", s == 201, b)
  return b

def confirm(order, partial=False):
  return call("POST", "/sales-orders/%s/confirm" % order["id"], "nina",
              {"allow_partial": partial})

def ship(order, *pairs):
  return call("POST", "/sales-orders/%s/shipments" % order["id"], "omar",
              {"lines": [{"so_line_id": i, "qty": q} for i, q in pairs]})

def look(sku="WIDGET"):
  return call("GET", "/inventory/skus/%s/availability?warehouse_id=WH-MAIN" % sku, "nina")[1]

def ledger(actor="tara"):
  return call("GET", "/ledger/inventory-reconciliation", actor)

def recon():
  s, b = ledger()
  ok("recon", s == 200, b)
  return b

def p1_rbac_and_sod():
  restart()
  order = po(4, 50_000)                            # 200000c, over the SoD threshold
  ok("p1.amount", order["amount_cents"] == 200_000)
  ok("p1.buyer_approve", approve(order, "ravi")[0] == 403)
  ok("p1.buyer_ledger", ledger("ravi")[0] == 403)
  ok("p1.acct_ledger", ledger()[0] == 200)
  ok("p1.unknown_404", call("GET", "/purchase-orders/%s/nope" % order["id"], "tara")[0] == 404)
  TOKENS["ghost"] = "not-a-real-token"
  ok("p1.bad_token_401", ledger("ghost")[0] == 401)
  # SoD below the API: in direct SQL the raiser still cannot approve.
  ok("p1.sod_sql", sql("UPDATE purchase_orders SET status='approved', approved_by=? WHERE id=?",
                       "ravi", order["id"]) == "IntegrityError")
  ok("p1.other_ok", sql("UPDATE purchase_orders SET status='approved', approved_by=? WHERE id=?",
                        "meera", order["id"]) is None)
  print("PROOF 1 RBAC   buyer->approve 403 | buyer->ledger 403 | accountant 200 | unknown "
        "route 404 | bad token 401 | self-approve 200000c blocked by CHECK in SQL")

def p2_immutable_ledger():
  restart()
  stock(4, 250)
  entry = erp.one(erp.connect(), "SELECT id FROM journal_entries LIMIT 1")["id"]
  blocked = [sql("UPDATE journal_entries SET source='x' WHERE id=?", entry),
             sql("DELETE FROM journal_entries WHERE id=?", entry),
             sql("UPDATE journal_lines SET debit_cents=1 WHERE entry_id=?", entry),
             sql("DELETE FROM journal_lines WHERE entry_id=?", entry),
             sql("UPDATE stock_movements SET qty=99"),
             sql("DELETE FROM stock_movements")]
  ok("p2.six_blocked", all(v == "IntegrityError" for v in blocked), blocked)
  s, rev = call("POST", "/ledger/journals/%s/reverse" % entry, "tara", {"reason": "wrong wh"})
  ok("p2.reversed", s == 201, rev)
  net = erp.one(erp.connect(), "SELECT COALESCE(SUM(debit_cents-credit_cents),0) AS n FROM "
                "journal_lines WHERE entry_id IN (?,?) AND account=?",
                entry, rev["reversal_id"], erp.ACC_INVENTORY)["n"]
  ok("p2.nets_zero", int(net) == 0, net)
  again = call("POST", "/ledger/journals/%s/reverse" % entry, "tara", {})
  ok("p2.no_double_reverse", again[0] == 409, again)
  print("PROOF 2 LEDGER UPDATE+DELETE rejected on entries/lines/movements 6/6 | reversal "
        "nets acct %s to 0c | re-reversal 409 %s" % (erp.ACC_INVENTORY, again[1]["error"]))

def p3_last_unit_race():
  restart()
  stock(1, 250)
  orders = [so(("WIDGET", 1, 400)) for _ in range(20)]
  gate = threading.Barrier(20)

  def racer(order):
    gate.wait()                                    # all 20 leave the client together
    return confirm(order)

  with ThreadPoolExecutor(max_workers=20) as pool:
    codes = [f.result()[0] for f in [pool.submit(racer, o) for o in orders]]
  peak, snap, report = server.IN_FLIGHT["peak"], look(), recon()
  ok("p3.one_winner", codes.count(200) == 1, codes)
  ok("p3.rest_conflict", codes.count(409) == 19, codes)
  ok("p3.overlapped", peak >= 2, peak)
  ok("p3.not_oversold", snap["reserved"] == 1.0 and snap["available"] == 0.0, snap)
  ok("p3.reconciles", report["ok"], report)
  print("PROOF 3 RACE   20 concurrent confirms vs 1 unit -> 200x%d 409x%d | peak in-flight %d "
        "(overlapped) | on_hand %s reserved %s | recon ok=%s"
        % (codes.count(200), codes.count(409), peak, snap["on_hand"], snap["reserved"],
           report["ok"]))

def p4_over_receipt():
  restart()
  order = po(10, 100, tol=0.1)
  approve(order)
  first = receive(order, 6)
  ok("p4.first", first[0] == 201, first)
  ok("p4.outstanding", abs(first[1]["lines"][0]["qty_outstanding"] - 4) < 1e-9, first)
  over = receive(order, 6)                         # 6 + 6 = 12, past the 11.0 ceiling
  ok("p4.rejected", over[0] == 409 and over[1]["error"] == "OVER_RECEIPT")
  ok("p4.ceiling", abs(over[1]["ceiling"] - 11.0) < 1e-9, over)
  ok("p4.inclusive", receive(order, 5)[0] == 201)  # 6 + 5 = 11.0 exactly
  ok("p4.past_ceiling", receive(order, 1)[0] == 409)
  snap, report = look(), recon()
  ok("p4.on_hand", abs(snap["on_hand"] - 11.0) < 1e-9, snap)
  ok("p4.no_trace", report["ok"], report)
  # Regression: two entries naming the same line in ONE request each measured
  # themselves against the stored qty_received, so 6+6 slipped past the ceiling.
  fresh = po(10, 100)
  approve(fresh)
  dup = receive(fresh, 6, 6)
  ok("p4.dup_line_rejected", dup[0] == 409 and dup[1]["error"] == "OVER_RECEIPT", dup)
  ok("p4.dup_line_at_ceiling", receive(fresh, 6, 4)[0] == 201)
  print("PROOF 4 OVERGR ordered 10 tol 0.1 -> ceiling 11.0 | 6 ok | +6 409 OVER_RECEIPT | +5 "
        "ok at ceiling | +1 409 | on_hand %s | same line twice in one request: 6+6 409, 6+4 201"
        % snap["on_hand"])

def p5_partial_shipment():
  restart()
  stock(3, 100)
  strict = confirm(so(("WIDGET", 10, 180)))
  ok("p5.strict_409", strict[0] == 409 and strict[1]["error"] == "INSUFFICIENT_STOCK")
  ok("p5.reserved_nothing", look()["reserved"] == 0.0)
  order = so(("WIDGET", 10, 180))
  partial = confirm(order, partial=True)
  ok("p5.partial_ok", partial[0] == 200, partial)
  line = partial[1]["lines"][0]
  ok("p5.3_and_7", line["qty_reserved"] == 3.0 and line["qty_backordered"] == 7.0, line)
  over = ship(order, (line["so_line_id"], 4))
  ok("p5.no_ship_backorder", over[0] == 409 and over[1]["error"] == "SHIP_EXCEEDS_RESERVED")
  done = ship(order, (line["so_line_id"], 2))
  ok("p5.shipped", done[0] == 201 and done[1]["status"] == "partially_shipped")
  snap, report = look(), recon()
  ok("p5.one_reserved", snap["reserved"] == 1.0, snap)
  ok("p5.reconciles", report["ok"], report)
  print("PROOF 5 PARTIAL on_hand 3 vs order 10 -> strict confirm 409 reserving nothing | "
        "allow_partial 3 reserved 7 backordered | ship 4 409, ship 2 -> partially_shipped | "
        "on_hand %s reserved %s" % (snap["on_hand"], snap["reserved"]))

def p6_valuation_and_reconciliation():
  restart()
  stock(10, 250)
  stock(4, 400)                                    # blended: 4100c across 14 units
  order = so(("WIDGET", 6, 900))
  confirm(order)
  done = ship(order, (order["lines"][0]["id"], 6))
  ok("p6.shipped", done[0] == 201, done)
  report, snap = recon(), look()
  conn = erp.connect()
  cogs = erp.Ledger.balance(conn, erp.ACC_COGS)
  revenue = -erp.Ledger.balance(conn, erp.ACC_REVENUE)
  held = report["I1_gl_vs_movements"]["gl_inventory_cents"]
  ok("p6.i1", report["I1_gl_vs_movements"]["delta_cents"] == 0, report)
  ok("p6.i2", report["I2_position_vs_movements"]["ok"])
  ok("p6.i3", report["I3_entries_balanced"]["ok"])
  ok("p6.revenue", revenue == 5400, revenue)
  ok("p6.on_hand", abs(snap["on_hand"] - 8.0) < 1e-9)
  ok("p6.no_rounding_loss", cogs + held == 4100, [cogs, held])
  print("PROOF 6 VALUE  10@250c + 4@400c = 4100c/14u | ship 6 -> COGS %dc + inventory %dc = "
        "4100c exactly | revenue %dc | I1 0c, I2 clean, I3 balanced" % (cogs, held, revenue))
  # Regression: two lines on one SKU used to price against the same pre-issue
  # value, breaking I1 by a cent. Also covers a zero-cost receipt, whose journal
  # would otherwise carry a 0/0 line and fail the one-sided CHECK.
  restart()
  stock(1, 4); stock(2, 3)                         # 3 units worth 10c -> forces rounding
  stock(5, 0, sku="GIZMO")
  two = so(("WIDGET", 1, 50), ("WIDGET", 1, 50))
  ids = [l["so_line_id"] for l in confirm(two)[1]["lines"]]
  ok("p6.same_sku_ship", ship(two, (ids[0], 1), (ids[1], 1))[0] == 201)
  ok("p6.same_sku_exact", recon()["I1_gl_vs_movements"]["delta_cents"] == 0, recon())
  ok("p6.free_receipt", look("GIZMO")["on_hand"] == 5.0 and recon()["ok"])
  print("PROOF 7 REGRSS duplicate receipt line 409 (was over-receiving) | two lines on one "
        "SKU I1 0c (was 1c off) | zero-cost receipt posts and reconciles")


if __name__ == "__main__":
  print("AUTOMATED TEST RESULTS")
  for proof in (p1_rbac_and_sod, p2_immutable_ledger, p3_last_unit_race, p4_over_receipt,
                p5_partial_shipment, p6_valuation_and_reconciliation):
    proof()
  print("ALL PROOFS PASSED")
