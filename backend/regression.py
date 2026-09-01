"""Regression suite for Stage 4's three seeded bugs and the transfer feature.

    python3 regression.py <erp build> <routes build> [label]
    python3 regression.py erp.py routes.py FIXED

Every scenario is run against BOTH the seeded build and the fixed build. A test
that only passes on the fixed build proves nothing on its own -- it has to fail
on the buggy one, or it is not testing the bug. The matrix in NOTES is the
output of this file against each build.

Two things about the harness itself, both learned the hard way:

1. It loads a build under its canonical module name via importlib instead of
   copying it over the source tree. The previous version did
   `shutil.copy(variant, "erp.py")`, which mutated the working tree and leaked
   across invocations: running BUG2 and then BUG3 left `erp.py` as the bug-2
   build, so the BUG3 run reported an R2 failure that belonged to the previous
   command. A harness that can corrupt its own inputs cannot be trusted about
   which build failed -- and that is the whole job of this file.

2. Each scenario provisions the stock it needs and asserts on *deltas*, not on
   absolute balances. The earlier version chained: R4 read the 10 units R2
   happened to leave behind, so reordering or dropping a test silently changed
   what the others meant.
"""
import importlib.util, json, sys, threading, urllib.error, urllib.request
from collections import Counter


def load(path, name):
  """Import `path` as module `name` without touching the file it stands in for."""
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  sys.modules[name] = module          # so `import erp` inside server.py finds this one
  spec.loader.exec_module(module)
  return module


class Harness:
  """One booted API plus the helpers every scenario needs."""

  def __init__(self, erp_path, routes_path):
    self.erp = load(erp_path, "erp")
    load(routes_path, "routes")
    import server                      # imports the two builds above from sys.modules
    self.server = server
    self.tokens = server.reset_database()
    self.httpd, self.port = server.serve(0)

  def stop(self):
    self.httpd.shutdown()

  def call(self, method, path, actor, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (self.port, path), data=data, method=method,
        headers={"Authorization": "Bearer " + self.tokens[actor],
                 "Content-Type": "application/json"})
    try:
      with urllib.request.urlopen(request, timeout=30) as response:
        return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
      return err.code, json.loads(err.read())

  def rows(self, sql, *args):
    conn = self.erp.connect()
    try:
      return [dict(r) for r in conn.execute(sql, args)]
    finally:
      conn.close()

  # -- fixtures ---------------------------------------------------------------

  def receive_stock(self, qty, unit_cost=100, sku="WIDGET", warehouse="WH-MAIN"):
    """Order, approve and fully receive `qty`, so a scenario owns its own stock."""
    po = self.call("POST", "/purchase-orders", "ravi",
                   {"warehouse_id": warehouse,
                    "lines": [{"sku": sku, "qty": qty, "unit_cost_cents": unit_cost}]})[1]
    self.call("POST", "/purchase-orders/%s/approve" % po["id"], "meera", {})
    self.call("POST", "/purchase-orders/%s/receipts" % po["id"], "kabir",
              {"warehouse_id": warehouse,
               "lines": [{"po_line_id": po["lines"][0]["id"], "qty": qty}]})
    return po

  def approved_po(self, qty, sku="GIZMO", unit_cost=100, warehouse="WH-MAIN"):
    """An approved PO with nothing received yet."""
    po = self.call("POST", "/purchase-orders", "ravi",
                   {"warehouse_id": warehouse,
                    "lines": [{"sku": sku, "qty": qty, "unit_cost_cents": unit_cost}]})[1]
    self.call("POST", "/purchase-orders/%s/approve" % po["id"], "meera", {})
    return po

  def position(self, sku="WIDGET", warehouse="WH-MAIN"):
    return self.call("GET", "/inventory/skus/%s/availability?warehouse_id=%s"
                     % (sku, warehouse), "nina")[1]

  def gl(self, account="1300"):
    return self.rows("SELECT COALESCE(SUM(debit_cents-credit_cents),0) AS b "
                     "FROM journal_lines WHERE account=?", account)[0]["b"]

  def reconciliation(self):
    return self.call("GET", "/ledger/inventory-reconciliation", "tara")[1]

  def movement_count(self):
    return len(self.rows("SELECT id FROM stock_movements"))

  def confirm_concurrently(self, order_ids, actor="nina"):
    """Release N confirms from a barrier so they genuinely overlap."""
    gate, codes, lock = threading.Barrier(len(order_ids)), [], threading.Lock()

    def run(order_id):
      gate.wait()
      status, body = self.call("POST", "/sales-orders/%s/confirm" % order_id, actor, {})
      with lock:
        codes.append("200" if status == 200 else "%s %s" % (status, body.get("error", "")))

    threads = [threading.Thread(target=run, args=(i,)) for i in order_ids]
    for t in threads:
      t.start()
    for t in threads:
      t.join()
    return codes


# -- scenarios ----------------------------------------------------------------
# Each returns (ok, detail). Registered in TESTS at the bottom.

def r1_reservation_race(h):
  """Bug 1: eight concurrent confirms must not all take the same single unit."""
  h.receive_stock(1, 250, sku="WIDGET")
  order_ids = [h.call("POST", "/sales-orders", "nina",
                      {"warehouse_id": "WH-MAIN",
                       "lines": [{"sku": "WIDGET", "qty": 1, "unit_price_cents": 900}]})[1]["id"]
               for _ in range(8)]
  codes = h.confirm_concurrently(order_ids)
  after = h.position("WIDGET")
  winners = codes.count("200")
  ok = winners == 1 and after["reserved"] <= after["on_hand"]
  return ok, ("%d winners for 1 unit, reserved=%s on_hand=%s, peak in-flight %d, codes=%s"
              % (winners, after["reserved"], after["on_hand"],
                 h.server.IN_FLIGHT["peak"], dict(Counter(codes))))


def r2_partial_receipt(h):
  """Bug 2: a SECOND partial receipt against one line must not double-count.

  The first receipt is accidentally correct on the seeded build, which is why
  this needs two.
  """
  po = h.approved_po(10, sku="GIZMO")
  line_id = po["lines"][0]["id"]

  def receive(qty):
    return h.call("POST", "/purchase-orders/%s/receipts" % po["id"], "kabir",
                  {"warehouse_id": "WH-MAIN", "lines": [{"po_line_id": line_id, "qty": qty}]})

  first, second = receive(6), receive(4)
  stored = float(h.rows("SELECT qty_received FROM po_lines WHERE id=?",
                        line_id)[0]["qty_received"])
  third = receive(1)
  ok = (first[0] == 201 and second[0] == 201 and stored == 10.0 and third[0] == 409)
  return ok, ("6 then 4 -> qty_received=%s (want 10), outstanding=%s, a further +1 -> %s %s"
              % (stored,
                 second[1]["lines"][0]["qty_outstanding"] if second[0] == 201 else "n/a",
                 third[0], third[1].get("error", "")))


def r2b_i4_catches_it(h):
  """The invariant that would have caught Bug 2 without a test looking for it.

  Bug 2 corrupted po_lines.qty_received while I1, I2 and I3 all stayed green,
  because movements are driven by the received qty and never read the column.
  I4 joins each line to the receipt movements carrying its id as `ref`, so a
  miscounted line is reported by reconciliation itself.
  """
  po = h.approved_po(10, sku="GIZMO")
  line_id = po["lines"][0]["id"]
  for qty in (6, 4):
    h.call("POST", "/purchase-orders/%s/receipts" % po["id"], "kabir",
           {"warehouse_id": "WH-MAIN", "lines": [{"po_line_id": line_id, "qty": qty}]})
  i4 = h.reconciliation()["I4_po_received_vs_movements"]
  offenders = [l for l in i4["miscounted_lines"] if l["po_line_id"] == line_id]
  detail = ("I4 ok=%s" % i4["ok"]) if i4["ok"] else (
      "I4 ok=False, this line qty_received=%s vs movements=%s"
      % (offenders[0]["qty_received"], offenders[0]["movement_qty"]) if offenders
      else "I4 ok=False on another line")
  return i4["ok"] and not offenders, detail


def r3_finance_authz(h):
  """Bug 3: an empty role set meant 'any authenticated caller'.

  Asserts the NEGATIVE half. A permission test that only checks the allowed
  path cannot fail open, which is exactly how this bug survived.
  """
  clerk_journals = h.call("GET", "/ledger/journals", "kabir")[0]
  clerk_recon = h.call("GET", "/ledger/inventory-reconciliation", "kabir")[0]
  accountant = h.call("GET", "/ledger/journals", "tara")[0]
  ok = clerk_journals == 403 and clerk_recon == 403 and accountant == 200
  return ok, ("warehouse_clerk /ledger/journals -> %d, /ledger/inventory-reconciliation -> %d, "
              "accountant -> %d" % (clerk_journals, clerk_recon, accountant))


def r4_transfer_is_a_pair(h):
  """The transfer is a linked pair of movements, never a balance edit."""
  h.receive_stock(4, 100, sku="GIZMO", warehouse="WH-MAIN")
  src_before = h.position("GIZMO", "WH-MAIN")["on_hand"]
  dst_before = h.position("GIZMO", "WH-WEST")["on_hand"]
  count_before = h.movement_count()
  status, _ = h.call("POST", "/inventory/transfers", "kabir",
                     {"sku": "GIZMO", "from_warehouse_id": "WH-MAIN",
                      "to_warehouse_id": "WH-WEST", "qty": 4})
  made = h.rows("SELECT kind, qty, value_cents, journal_id FROM stock_movements "
                "ORDER BY rowid DESC LIMIT 2")
  paired = (len(made) == 2 and made[0]["journal_id"] == made[1]["journal_id"]
            and {m["kind"] for m in made} == {"issue", "receipt"}
            and made[0]["qty"] == -made[1]["qty"]
            and made[0]["value_cents"] == -made[1]["value_cents"])
  src_after = h.position("GIZMO", "WH-MAIN")["on_hand"]
  dst_after = h.position("GIZMO", "WH-WEST")["on_hand"]
  recon = h.reconciliation()
  ok = (status == 201 and paired and h.movement_count() == count_before + 2
        and src_after == src_before - 4 and dst_after == dst_before + 4 and recon["ok"])
  return ok, ("status %s, +%d movements sharing journal %s, src %s->%s dst %s->%s, recon ok=%s"
              % (status, h.movement_count() - count_before,
                 (made[0]["journal_id"] or "")[:8], src_before, src_after,
                 dst_before, dst_after, recon["ok"]))


def r4b_transfer_bounds(h):
  """Bounded by AVAILABLE, not on_hand: reserved stock cannot be moved away."""
  h.receive_stock(5, 100, sku="GIZMO", warehouse="WH-MAIN")
  available = h.position("GIZMO", "WH-MAIN")["available"]
  status, body = h.call("POST", "/inventory/transfers", "kabir",
                        {"sku": "GIZMO", "from_warehouse_id": "WH-MAIN",
                         "to_warehouse_id": "WH-WEST", "qty": available + 1})
  return status == 409, ("transferring %s with %s available -> %s %s"
                         % (available + 1, available, status, body.get("error", "")))


def r4c_transfer_edges(h):
  """The boundaries nothing tested until I attacked my own feature.

  `unknown warehouse` is the one that mattered: it used to answer
  409 INTEGRITY_VIOLATION -- a foreign key leaking to a caller who asked a
  domain question, which is the exact failure Bug 1's write-up criticises.
  """
  h.receive_stock(6, 100, sku="GIZMO", warehouse="WH-MAIN")

  def transfer(actor="kabir", **overrides):
    body = {"sku": "GIZMO", "from_warehouse_id": "WH-MAIN",
            "to_warehouse_id": "WH-WEST", "qty": 1}
    body.update(overrides)
    return h.call("POST", "/inventory/transfers", actor, body)[0]

  edges = [("sales_rep denied", transfer(actor="nina"), 403),
           ("accountant denied", transfer(actor="tara"), 403),
           ("same warehouse", transfer(to_warehouse_id="WH-MAIN"), 400),
           ("unknown warehouse", transfer(to_warehouse_id="WH-NOPE"), 404),
           ("unknown sku", transfer(sku="NOSUCH"), 404),
           ("negative qty", transfer(qty=-5), 400)]
  ok = all(got == want for _, got, want in edges)
  return ok, ", ".join("%s->%d" % (name, got) for name, got, _ in edges)


def r4d_round_trip(h):
  """Out and back conserves stock and leaves GL 1300 byte-identical.

  This is the concrete form of "a transfer relocates value without revaluing
  it": the entry exists as an audit anchor but carries no lines.
  """
  h.receive_stock(10, 125, sku="GIZMO", warehouse="WH-MAIN")
  src_before = h.position("GIZMO", "WH-MAIN")["on_hand"]
  dst_before = h.position("GIZMO", "WH-WEST")["on_hand"]
  gl_before = h.gl()
  h.call("POST", "/inventory/transfers", "kabir",
         {"sku": "GIZMO", "from_warehouse_id": "WH-MAIN",
          "to_warehouse_id": "WH-WEST", "qty": 6})
  back, _ = h.call("POST", "/inventory/transfers", "kabir",
                   {"sku": "GIZMO", "from_warehouse_id": "WH-WEST",
                    "to_warehouse_id": "WH-MAIN", "qty": dst_before + 6})
  src_after = h.position("GIZMO", "WH-MAIN")["on_hand"]
  dst_after = h.position("GIZMO", "WH-WEST")["on_hand"]
  gl_after = h.gl()
  recon = h.reconciliation()
  ok = (back == 201 and src_after == src_before + dst_before and dst_after == 0.0
        and gl_before == gl_after and recon["ok"])
  return ok, ("out 6 then back %s -> src %s->%s dst %s->%s, GL 1300 %sc -> %sc, recon ok=%s"
              % (dst_before + 6, src_before, src_after, dst_before, dst_after,
                 gl_before, gl_after, recon["ok"]))


def r5_concurrent_transfers(h):
  """Stage 4 asserted the transfer's concurrency safety in a COMMENT and never
  exercised it. Eight concurrent transfers, each of a third of the stock, so
  exactly three can succeed: stock must be conserved across the winners and
  reconciliation must still be clean. This is the test that comment stood in for.

  It also catches Bug 1, which was not the intent. On the deferred-BEGIN build
  seven of the eight come back 500 OperationalError, because SQLite will not
  upgrade a deferred read transaction to a writer once another has committed
  underneath it, and busy_timeout deliberately does not retry that case.
  """
  h.receive_stock(30, 100, sku="GIZMO", warehouse="WH-MAIN")
  src_before = h.position("GIZMO", "WH-MAIN")["on_hand"]
  dst_before = h.position("GIZMO", "WH-WEST")["on_hand"]
  # Size the transfer from what is actually on hand rather than a magic number,
  # so the scenario stays honest wherever it sits in the run order: eight
  # attempts at a third of the stock each means exactly three can win.
  attempts = 8
  each = max(1.0, float(src_before) // 3)
  expected_winners = int(src_before // each)
  gate, codes, lock = threading.Barrier(attempts), [], threading.Lock()

  def run():
    gate.wait()
    status, body = h.call("POST", "/inventory/transfers", "kabir",
                          {"sku": "GIZMO", "from_warehouse_id": "WH-MAIN",
                           "to_warehouse_id": "WH-WEST", "qty": each})
    with lock:
      codes.append("201" if status == 201 else "%s %s" % (status, body.get("error", "")))

  threads = [threading.Thread(target=run) for _ in range(attempts)]
  for t in threads:
    t.start()
  for t in threads:
    t.join()
  winners = codes.count("201")
  src_after = h.position("GIZMO", "WH-MAIN")["on_hand"]
  dst_after = h.position("GIZMO", "WH-WEST")["on_hand"]
  recon = h.reconciliation()
  conserved = (src_after == src_before - winners * each
               and dst_after == dst_before + winners * each)
  ok = winners == expected_winners and conserved and recon["ok"] and src_after >= 0
  return ok, ("%d of %d transfers of %d won against %s available (want %d), "
              "src %s->%s dst %s->%s, peak in-flight %d, recon ok=%s, codes=%s"
              % (winners, attempts, each, src_before, expected_winners, src_before,
                 src_after, dst_before, dst_after, h.server.IN_FLIGHT["peak"],
                 recon["ok"], dict(Counter(codes))))


TESTS = [
    ("R1  reservation race", r1_reservation_race),
    ("R2  partial receipt", r2_partial_receipt),
    ("R2b I4 catches bug 2", r2b_i4_catches_it),
    ("R3  finance authz", r3_finance_authz),
    ("R4  transfer is a pair", r4_transfer_is_a_pair),
    ("R4b transfer bounds", r4b_transfer_bounds),
    ("R4c transfer edges", r4c_transfer_edges),
    ("R4d round trip", r4d_round_trip),
    ("R5  concurrent transfers", r5_concurrent_transfers),
]


def main(argv):
  if len(argv) < 3:
    raise SystemExit(__doc__.strip().splitlines()[2].strip())
  harness = Harness(argv[1], argv[2])
  label = argv[3] if len(argv) > 3 else argv[1]
  results = []
  try:
    for name, test in TESTS:
      try:
        ok, detail = test(harness)
      except Exception as err:            # a crash is a failure, with its cause
        ok, detail = False, "raised %s: %s" % (type(err).__name__, err)
      results.append((name, ok, detail))
  finally:
    harness.stop()
  print("=== %s" % label)
  for name, ok, detail in results:
    print("  %-26s %s  %s" % (name, "PASS" if ok else "FAIL", detail))
  return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
  sys.exit(main(sys.argv))
