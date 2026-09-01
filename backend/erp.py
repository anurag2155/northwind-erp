"""Mini-ERP domain: schema, ledger, inventory, procurement, sales, reports.
The only module that touches the database; server.py is transport, proofs.py the
suite. Design rationale is in NOTES.
"""
import collections, sqlite3, time, uuid

import os
# ERP_DB lets a container mount the database on a volume.
DB_PATH = os.environ.get("ERP_DB", "/tmp/mini_erp.db")
ACC_INVENTORY, ACC_GRNI, ACC_COGS, ACC_AR, ACC_REVENUE = "1300", "2100", "5000", "1100", "4000"
SOD_THRESHOLD_CENTS = 100000
EPS = 1e-9

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE users (id TEXT PRIMARY KEY, role TEXT NOT NULL);
CREATE TABLE sessions (token TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id));
CREATE TABLE warehouses (id TEXT PRIMARY KEY);
CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE positions (
sku TEXT NOT NULL REFERENCES products(sku),
warehouse_id TEXT NOT NULL REFERENCES warehouses(id), on_hand REAL NOT NULL DEFAULT 0,
reserved REAL NOT NULL DEFAULT 0, value_cents INTEGER NOT NULL DEFAULT 0,
version INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (sku, warehouse_id),
CHECK (on_hand >= 0), CHECK (reserved >= 0), CHECK (value_cents >= 0),
CHECK (reserved <= on_hand));
CREATE TABLE journal_entries (
id TEXT PRIMARY KEY, source TEXT NOT NULL, source_id TEXT,
reverses_id TEXT REFERENCES journal_entries(id),
posted_by TEXT NOT NULL REFERENCES users(id), posted_at TEXT NOT NULL);
CREATE UNIQUE INDEX ux_reverse_once ON journal_entries(reverses_id) WHERE reverses_id IS NOT NULL;
CREATE TABLE journal_lines (
id TEXT PRIMARY KEY, entry_id TEXT NOT NULL REFERENCES journal_entries(id),
account TEXT NOT NULL, debit_cents INTEGER NOT NULL DEFAULT 0 CHECK (debit_cents >= 0),
credit_cents INTEGER NOT NULL DEFAULT 0 CHECK (credit_cents >= 0),
CHECK ((debit_cents > 0) <> (credit_cents > 0)));
CREATE TABLE stock_movements (
id TEXT PRIMARY KEY, sku TEXT NOT NULL, warehouse_id TEXT NOT NULL,
kind TEXT NOT NULL CHECK (kind IN ('receipt','issue','adjust')),
qty REAL NOT NULL CHECK (qty <> 0), value_cents INTEGER NOT NULL,
journal_id TEXT NOT NULL REFERENCES journal_entries(id), ref TEXT NOT NULL,
FOREIGN KEY (sku, warehouse_id) REFERENCES positions(sku, warehouse_id));
CREATE TABLE purchase_orders (
id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK (status IN ('submitted','approved','closed')),
warehouse_id TEXT NOT NULL REFERENCES warehouses(id),
created_by TEXT NOT NULL REFERENCES users(id), approved_by TEXT REFERENCES users(id),
amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
over_tolerance REAL NOT NULL DEFAULT 0 CHECK (over_tolerance >= 0),
CHECK (status <> 'approved' OR approved_by IS NOT NULL),
CHECK (approved_by IS NULL OR amount_cents < 100000 OR approved_by <> created_by));
CREATE TABLE po_lines (
id TEXT PRIMARY KEY, po_id TEXT NOT NULL REFERENCES purchase_orders(id),
sku TEXT NOT NULL REFERENCES products(sku), qty_ordered REAL NOT NULL CHECK (qty_ordered > 0),
qty_received REAL NOT NULL DEFAULT 0 CHECK (qty_received >= 0),
unit_cost_cents INTEGER NOT NULL CHECK (unit_cost_cents >= 0));
CREATE TABLE sales_orders (
id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK (status IN
('draft','confirming','confirmed','partially_shipped','shipped')),
warehouse_id TEXT NOT NULL REFERENCES warehouses(id),
created_by TEXT NOT NULL REFERENCES users(id));
CREATE TABLE so_lines (
id TEXT PRIMARY KEY, so_id TEXT NOT NULL REFERENCES sales_orders(id),
sku TEXT NOT NULL REFERENCES products(sku), qty_ordered REAL NOT NULL CHECK (qty_ordered > 0),
qty_reserved REAL NOT NULL DEFAULT 0 CHECK (qty_reserved >= 0),
qty_shipped REAL NOT NULL DEFAULT 0 CHECK (qty_shipped >= 0),
qty_backordered REAL NOT NULL DEFAULT 0 CHECK (qty_backordered >= 0),
unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
CHECK (qty_shipped <= qty_reserved));
CREATE TRIGGER je_no_upd BEFORE UPDATE ON journal_entries BEGIN SELECT RAISE(ABORT,'LEDGER_IMMUTABLE'); END;
CREATE TRIGGER je_no_del BEFORE DELETE ON journal_entries BEGIN SELECT RAISE(ABORT,'LEDGER_IMMUTABLE'); END;
CREATE TRIGGER jl_no_upd BEFORE UPDATE ON journal_lines BEGIN SELECT RAISE(ABORT,'LEDGER_IMMUTABLE'); END;
CREATE TRIGGER jl_no_del BEFORE DELETE ON journal_lines BEGIN SELECT RAISE(ABORT,'LEDGER_IMMUTABLE'); END;
CREATE TRIGGER mv_no_upd BEFORE UPDATE ON stock_movements BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
CREATE TRIGGER mv_no_del BEFORE DELETE ON stock_movements BEGIN SELECT RAISE(ABORT,'APPEND_ONLY'); END;
"""


class ApiError(Exception):
  """Carries its own HTTP status; nothing downstream matches message text."""
  def __init__(self, status, code, **detail):
    super().__init__(code)
    self.status, self.body = status, {"error": code, **detail}


def _id():
  return uuid.uuid4().hex[:12]


def connect():
  conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None, check_same_thread=False)
  conn.row_factory = sqlite3.Row
  conn.execute("PRAGMA foreign_keys = ON"); conn.execute("PRAGMA busy_timeout = 30000")
  return conn


def one(conn, sql, *args):
  return conn.execute(sql, args).fetchone()


def _line_out(line_id, spec):
  return {"id": line_id, "sku": spec["sku"], "qty_ordered": float(spec["qty"])}


def in_transaction(fn):
  # BEGIN IMMEDIATE: SQLite takes the writer lock at BEGIN, not at first write,
  # so two concurrent reserves cannot both read `available` before either
  # writes. That, not the CAS, makes the race safe; the CAS ports it.
  conn = connect()
  try:
    conn.execute("BEGIN IMMEDIATE")
    try:
      out = fn(conn)
      conn.execute("COMMIT")
      return out
    except Exception:
      conn.execute("ROLLBACK")
      raise
  finally:
    conn.close()


class Ledger:
  @staticmethod
  def post(conn, source, source_id, user_id, lines, reverses_id=None):
    # [(account, debit, credit)] in cents, must balance. Zero-value pairs are
    # dropped: a 0/0 line fails the one-sided CHECK and records nothing.
    lines = [l for l in lines if l[1] or l[2]]
    debits, credits = sum(l[1] for l in lines), sum(l[2] for l in lines)
    if debits != credits:
      raise ApiError(500, "UNBALANCED_JOURNAL", debits=debits, credits=credits)
    eid, at = _id(), time.strftime("%Y-%m-%dT%H:%M:%S")
    conn.execute("INSERT INTO journal_entries (id, source, source_id, reverses_id, "
                 "posted_by, posted_at) VALUES (?,?,?,?,?,?)",
                 (eid, source, source_id, reverses_id, user_id, at))
    for account, debit, credit in lines:
      conn.execute("INSERT INTO journal_lines (id, entry_id, account, debit_cents, "
                   "credit_cents) VALUES (?,?,?,?,?)", (_id(), eid, account, debit, credit))
    return eid

  @staticmethod
  def reverse(conn, entry_id, user_id, reason):
    rows = conn.execute("SELECT account, debit_cents, credit_cents FROM journal_lines WHERE "
                        "entry_id=?", (entry_id,)).fetchall()
    if not rows:
      raise ApiError(404, "JOURNAL_NOT_FOUND", journal_id=entry_id)
    prior = one(conn, "SELECT id FROM journal_entries WHERE reverses_id=?", entry_id)
    if prior:
      raise ApiError(409, "ALREADY_REVERSED", reversal_id=prior["id"])
    swapped = [(r["account"], r["credit_cents"], r["debit_cents"]) for r in rows]
    rid = Ledger.post(conn, "reversal", entry_id, user_id, swapped, entry_id)
    return {"reversal_id": rid, "reverses": entry_id, "reason": reason}

  @staticmethod
  def balance(conn, account):
    return int(one(conn, "SELECT COALESCE(SUM(debit_cents-credit_cents),0) AS b FROM "
                         "journal_lines WHERE account=?", account)["b"])


# A position holds three quantities, so a change to one is a triple. Naming it
# keeps `_apply` and `_move` to a single argument instead of three positional
# floats whose order a caller can silently transpose.
Delta = collections.namedtuple("Delta", "on_hand reserved value")


class Inventory:                          # the only writer of positions/movements
  @staticmethod
  def _row(conn, sku, wh, create=False):
    if create:
      conn.execute("INSERT OR IGNORE INTO positions (sku, warehouse_id) VALUES (?,?)", (sku, wh))
    row = one(conn, "SELECT * FROM positions WHERE sku=? AND warehouse_id=?", sku, wh)
    if row is None:
      raise ApiError(404, "NO_POSITION", sku=sku, warehouse_id=wh)
    return row

  @staticmethod
  def _apply(conn, row, delta):
    """Add `delta` to one position. The only statement that writes `positions`.

    Guarded by a compare-and-swap on `version`, which is inert under SQLite's
    single writer lock and becomes the concurrency control on Postgres.
    """
    if conn.execute("UPDATE positions SET on_hand=?, reserved=?, value_cents=?, "
                    "version=version+1 WHERE sku=? AND warehouse_id=? AND version=?",
                    (float(row["on_hand"]) + delta.on_hand,
                     float(row["reserved"]) + delta.reserved,
                     int(row["value_cents"]) + delta.value,
                     row["sku"], row["warehouse_id"], row["version"])).rowcount != 1:
      raise ApiError(409, "POSITION_CONFLICT", sku=row["sku"])

  @staticmethod
  def snapshot(conn, sku, wh):
    row = one(conn, "SELECT * FROM positions WHERE sku=? AND warehouse_id=?", sku, wh)
    on_hand, reserved, value = ((float(row["on_hand"]), float(row["reserved"]),
                                 int(row["value_cents"])) if row else (0.0, 0.0, 0))
    return {"sku": sku, "warehouse_id": wh, "on_hand": on_hand, "reserved": reserved,
            "available": on_hand - reserved,
            "unit_cost_cents": int(value / on_hand) if on_hand else 0}

  @staticmethod
  def _move(conn, row, kind, delta, journal_id, ref):
    """Apply `delta` and append the movement that records it. Signed: positive
    moves stock in, negative out. Knows nothing about reservations, unit costs
    or documents -- callers layer those rules on top.

    `delta.reserved` is part of the same triple rather than a separate call
    because `_apply` is a CAS on `version`: a row is written once per
    transaction, so a reservation release travels with the movement fulfilling
    it. `reserve` calls `_apply` directly with no movement, which is the
    schema-level rule that reservations move no value.
    """
    Inventory._apply(conn, row, delta)
    conn.execute("INSERT INTO stock_movements (id, sku, warehouse_id, kind, qty, "
                 "value_cents, journal_id, ref) VALUES (?,?,?,?,?,?,?,?)",
                 (_id(), row["sku"], row["warehouse_id"], kind,
                  delta.on_hand, delta.value, journal_id, ref))

  @staticmethod
  def receive(conn, sku, wh, qty, unit_cost_cents, journal_id, ref):
    row = Inventory._row(conn, sku, wh, create=True)
    value = int(round(qty * unit_cost_cents))
    Inventory._move(conn, row, "receipt", Delta(qty, 0.0, value), journal_id, ref)
    return value

  @staticmethod
  def reserve(conn, sku, wh, qty):
    row = Inventory._row(conn, sku, wh, create=True)
    take = min(qty, max(float(row["on_hand"]) - float(row["reserved"]), 0.0))
    if take > 0:
      Inventory._apply(conn, row, Delta(0.0, take, 0))   # no movement: no value moved
    return take

  @staticmethod
  def issue(conn, sku, wh, qty, value, journal_id, ref):
    """Fulfil a reservation: `_move` out, plus the rule that it must be reserved.

    That rule is the whole of what `issue` adds over `_move`, which is why a
    transfer -- which reserves nothing -- goes straight to `_move` instead of
    being forced through here.
    """
    # `value` comes from the caller, which prices a shipment line by line.
    # Recomputing here let two lines on one SKU take the same pre-issue value.
    row = Inventory._row(conn, sku, wh)
    on_hand, reserved = float(row["on_hand"]), float(row["reserved"])
    if qty > on_hand + EPS or qty > reserved + EPS:
      raise ApiError(409, "NOT_RESERVED", sku=sku, reserved=reserved)
    Inventory._move(conn, row, "issue", Delta(-qty, -qty, -value), journal_id, ref)
    return value


  @staticmethod
  def transfer(conn, user, body):
    """Move stock between warehouses as a linked PAIR of movements -- an issue at
    the source and a receipt at the destination sharing one journal entry --
    never by editing a balance. Value travels with the goods at the source's
    blended rate, so the two rows net to zero and I1 is undisturbed.

    The journal entry carries no lines on purpose: inventory is a single GL
    account across warehouses, so a transfer relocates value without revaluing
    it. The entry still exists because stock_movements.journal_id is NOT NULL --
    every movement stays anchored to an auditable event.
    """
    sku, src, dst = body["sku"], body["from_warehouse_id"], body["to_warehouse_id"]
    qty = float(body["qty"])
    if src == dst:
      raise ApiError(400, "SAME_WAREHOUSE", warehouse_id=src)
    if qty <= 0:
      raise ApiError(400, "NON_POSITIVE_QTY")
    # An unknown destination would otherwise surface as the foreign key firing,
    # i.e. a constraint leak where the caller asked a domain question.
    if not one(conn, "SELECT id FROM warehouses WHERE id=?", dst):
      raise ApiError(404, "WAREHOUSE_NOT_FOUND", warehouse_id=dst)
    # Fixed order, so opposing transfers of one SKU touch the rows in the same
    # sequence. Under SQLite's single writer lock this is documentation rather
    # than a lock; on Postgres this loop is where SELECT ... FOR UPDATE goes.
    for key in sorted([(sku, src), (sku, dst)]):
      Inventory._row(conn, key[0], key[1], create=(key[1] == dst))
    out = Inventory._row(conn, sku, src)
    on_hand, reserved = float(out["on_hand"]), float(out["reserved"])
    if qty > on_hand - reserved + EPS:
      raise ApiError(409, "INSUFFICIENT_STOCK", sku=sku, warehouse_id=src,
                     available=on_hand - reserved)
    value = int(round(int(out["value_cents"]) * qty / on_hand)) if on_hand else 0
    journal_id = Ledger.post(conn, "transfer", f"{src}->{dst}", user["id"], [])
    # Two _move calls with opposite signs. Stage 4 hand-wrote both sides of this
    # against the raw tables; now the pair is visibly a pair, and neither side
    # can drift from how receipts and issues maintain a position.
    Inventory._move(conn, out, "issue", Delta(-qty, 0.0, -value),
                    journal_id, f"transfer:{dst}")
    into = Inventory._row(conn, sku, dst, create=True)
    Inventory._move(conn, into, "receipt", Delta(qty, 0.0, value),
                    journal_id, f"transfer:{src}")
    return {"sku": sku, "from_warehouse_id": src, "to_warehouse_id": dst, "qty": qty,
            "value_cents": value, "journal_id": journal_id}


class Procurement:
  @staticmethod
  def create_po(conn, user, body):
    lines = body.get("lines") or []
    if not lines:
      raise ApiError(400, "NO_LINES")
    po_id, out = _id(), []
    amount = sum(int(round(l["qty"] * l["unit_cost_cents"])) for l in lines)
    conn.execute("INSERT INTO purchase_orders (id, status, warehouse_id, created_by, "
                 "approved_by, amount_cents, over_tolerance) VALUES (?,?,?,?,?,?,?)",
                 (po_id, "submitted", body["warehouse_id"], user["id"], None,
                  amount, float(body.get("over_tolerance", 0))))
    for l in lines:
      lid = _id()
      conn.execute("INSERT INTO po_lines (id, po_id, sku, qty_ordered, qty_received, "
                   "unit_cost_cents) VALUES (?,?,?,?,0,?)",
                   (lid, po_id, l["sku"], float(l["qty"]), int(l["unit_cost_cents"])))
      out.append(_line_out(lid, l))
    return {"id": po_id, "status": "submitted", "amount_cents": amount, "lines": out}

  @staticmethod
  def approve_po(conn, user, po_id):
    po = one(conn, "SELECT * FROM purchase_orders WHERE id=?", po_id)
    if po is None:
      raise ApiError(404, "PO_NOT_FOUND", po_id=po_id)
    if po["status"] != "submitted":
      raise ApiError(409, "PO_NOT_SUBMITTED", status=po["status"])
    if po["created_by"] == user["id"] and int(po["amount_cents"]) >= SOD_THRESHOLD_CENTS:
      raise ApiError(403, "SOD_SELF_APPROVAL", threshold_cents=SOD_THRESHOLD_CENTS)
    conn.execute("UPDATE purchase_orders SET status='approved', approved_by=? WHERE id=? "
                 "AND status='submitted'", (user["id"], po_id))
    return {"id": po_id, "status": "approved", "approved_by": user["id"]}

  @staticmethod
  def receive(conn, user, po_id, body):
    po = one(conn, "SELECT * FROM purchase_orders WHERE id=?", po_id)
    if po is None:
      raise ApiError(404, "PO_NOT_FOUND", po_id=po_id)
    if po["status"] != "approved":
      raise ApiError(409, "PO_NOT_APPROVED", status=po["status"])
    wh = body.get("warehouse_id") or po["warehouse_id"]
    planned, journal, results, seen = [], [], [], {}
    for item in body["lines"]:
      line = one(conn, "SELECT * FROM po_lines WHERE id=? AND po_id=?",
                 item["po_line_id"], po_id)
      if line is None:
        raise ApiError(404, "PO_LINE_NOT_FOUND", id=item["po_line_id"])
      qty = float(item["qty"])
      if qty <= 0:
        raise ApiError(400, "NON_POSITIVE_QTY")
      # `seen` accumulates within the request: two entries naming one line must
      # not both measure against the stored quantity.
      ordered = float(line["qty_ordered"])
      already = float(line["qty_received"]) + seen.get(line["id"], 0.0)
      ceiling = ordered * (1.0 + float(po["over_tolerance"]))
      if already + qty > ceiling + EPS:
        raise ApiError(409, "OVER_RECEIPT", po_line_id=line["id"], qty_received=already,
                       ceiling=ceiling)
      value = int(round(qty * int(line["unit_cost_cents"])))
      journal += [(ACC_INVENTORY, value, 0), (ACC_GRNI, 0, value)]
      seen[line["id"]] = seen.get(line["id"], 0.0) + qty
      planned.append((line, qty))
      results.append({"po_line_id": line["id"], "qty_received": qty,
                      "qty_outstanding": max(ordered - already - qty, 0.0)})
    # The receipt IS its journal entry: source_id is the PO, posted_by the
    # receiver, movements carry warehouse and qty.
    journal_id = Ledger.post(conn, "goods_receipt", po_id, user["id"], journal)
    for line, qty in planned:
      conn.execute("UPDATE po_lines SET qty_received = qty_received + ? WHERE id=?",
                   (qty, line["id"]))
      Inventory.receive(conn, line["sku"], wh, qty, int(line["unit_cost_cents"]),
                        journal_id, line["id"])
    return {"gr_id": journal_id, "journal_id": journal_id, "lines": results}


class Sales:
  @staticmethod
  def create_so(conn, user, body):
    lines = body.get("lines") or []
    if not lines:
      raise ApiError(400, "NO_LINES")
    so_id, out = _id(), []
    conn.execute("INSERT INTO sales_orders (id, status, warehouse_id, created_by) "
                 "VALUES (?,?,?,?)",
                 (so_id, "draft", body["warehouse_id"], user["id"]))
    for l in lines:
      lid = _id()
      conn.execute("INSERT INTO so_lines (id, so_id, sku, qty_ordered, qty_reserved, "
                   "qty_shipped, qty_backordered, unit_price_cents) "
                   "VALUES (?,?,?,?,0,0,0,?)",
                   (lid, so_id, l["sku"], float(l["qty"]), int(l["unit_price_cents"])))
      out.append(_line_out(lid, l))
    return {"id": so_id, "status": "draft", "lines": out}

  @staticmethod
  def confirm_so(conn, user, so_id, body):
    # Conditional UPDATE: concurrent confirms diverge before touching stock.
    if conn.execute("UPDATE sales_orders SET status='confirming' WHERE id=? AND "
                    "status='draft'", (so_id,)).rowcount != 1:
      row = one(conn, "SELECT status FROM sales_orders WHERE id=?", so_id)
      if row is None:
        raise ApiError(404, "SO_NOT_FOUND", so_id=so_id)
      raise ApiError(409, "SO_NOT_DRAFT", status=row["status"])
    so = one(conn, "SELECT * FROM sales_orders WHERE id=?", so_id)
    allow_partial, results = bool(body.get("allow_partial")), []
    for line in conn.execute("SELECT * FROM so_lines WHERE so_id=? ORDER BY sku",
                             (so_id,)).fetchall():
      want = float(line["qty_ordered"])
      taken = Inventory.reserve(conn, line["sku"], so["warehouse_id"], want)
      if taken + EPS < want and not allow_partial:
        raise ApiError(409, "INSUFFICIENT_STOCK", sku=line["sku"], available=taken)
      conn.execute("UPDATE so_lines SET qty_reserved=?, qty_backordered=? WHERE id=?",
                   (taken, want - taken, line["id"]))
      results.append({"so_line_id": line["id"], "sku": line["sku"], "qty_reserved": taken,
                      "qty_backordered": want - taken})
    conn.execute("UPDATE sales_orders SET status = 'confirmed' WHERE id=?", (so_id,))
    return {"id": so_id, "status": "confirmed", "lines": results}

  @staticmethod
  def ship_so(conn, user, so_id, body):
    so = one(conn, "SELECT * FROM sales_orders WHERE id=?", so_id)
    if so is None:
      raise ApiError(404, "SO_NOT_FOUND", so_id=so_id)
    if so["status"] not in ("confirmed", "partially_shipped"):
      raise ApiError(409, "SO_NOT_SHIPPABLE", status=so["status"])
    journal, planned, pos, priced = [], [], {}, []
    for item in body["lines"]:
      qty = float(item["qty"])
      if qty <= 0:
        raise ApiError(400, "NON_POSITIVE_QTY")
      # Conditional UPDATE: ship at most what is still reserved.
      if conn.execute("UPDATE so_lines SET qty_shipped = qty_shipped + ? WHERE id=? AND "
                      "so_id=? AND qty_reserved - qty_shipped >= ?",
                      (qty, item["so_line_id"], so_id, qty)).rowcount != 1:
        raise ApiError(409, "SHIP_EXCEEDS_RESERVED", so_line_id=item["so_line_id"])
      planned.append((one(conn, "SELECT * FROM so_lines WHERE id=?", item["so_line_id"]), qty))
    # Priced against a running position, not the stored one.
    for line, qty in planned:
      sku = line["sku"]
      if sku not in pos:
        r = one(conn, "SELECT on_hand, value_cents FROM positions WHERE sku=? AND warehouse_id=?",
                sku, so["warehouse_id"])
        pos[sku] = [float(r["on_hand"]), int(r["value_cents"])] if r else [0.0, 0]
      on_hand, value = pos[sku]
      cogs = int(round(value * qty / on_hand)) if on_hand > 0 else 0
      pos[sku] = [on_hand - qty, value - cogs]
      revenue = int(round(qty * int(line["unit_price_cents"])))
      journal += [(ACC_COGS, cogs, 0), (ACC_INVENTORY, 0, cogs),
                  (ACC_AR, revenue, 0), (ACC_REVENUE, 0, revenue)]
      priced.append((line, qty, cogs))
    journal_id = Ledger.post(conn, "shipment", so_id, user["id"], journal)
    for line, qty, cogs in priced:
      Inventory.issue(conn, line["sku"], so["warehouse_id"], qty, cogs, journal_id, line["id"])
    left = float(one(conn, "SELECT COALESCE(SUM(qty_ordered-qty_shipped),0) AS l FROM "
                           "so_lines WHERE so_id=?", so_id)["l"])
    status = "shipped" if left <= EPS else "partially_shipped"
    conn.execute("UPDATE sales_orders SET status=? WHERE id=?", (status, so_id))
    return {"so_id": so_id, "status": status, "journal_id": journal_id}


class Reports:
  @staticmethod
  def reconcile(conn):
    """I1 ledger vs sub-ledger, I2 position vs movements, I3 entries balanced,
    I4 PO receipt ledger vs movements. Separate flags, because one would hide
    which invariant broke.

    I4 exists because of Stage 4's Bug 2, which added an absolute quantity where
    a delta belonged: a second partial receipt left `qty_received` at 16 for 10
    units actually received, and I1, I2 and I3 all stayed green -- movements are
    driven by the received qty and never read the column. The invariant set tied
    the general ledger to the stock sub-ledger and left the purchase-order
    ledger unchecked. Diagnosing that in a write-up did not close it; this does.
    """
    gl = Ledger.balance(conn, ACC_INVENTORY)
    moved = int(one(conn, "SELECT COALESCE(SUM(value_cents),0) AS v FROM stock_movements")["v"])
    drifted = [{"sku": r["sku"], "wh": r["warehouse_id"],
                "position": [float(r["on_hand"]), int(r["value_cents"])],
                "movements": [float(r["mq"]), int(r["mv"])]}
               for r in conn.execute(
                   "SELECT p.sku, p.warehouse_id, p.on_hand, p.value_cents, "
                   "COALESCE(m.q, 0) AS mq, COALESCE(m.v, 0) AS mv FROM positions p LEFT JOIN "
                   "(SELECT sku, warehouse_id, SUM(qty) AS q, SUM(value_cents) AS v "
                   "FROM stock_movements GROUP BY sku, warehouse_id) m "
                   "ON m.sku = p.sku AND m.warehouse_id = p.warehouse_id")
               if abs(float(r["on_hand"]) - float(r["mq"])) > EPS
               or int(r["value_cents"]) != int(r["mv"])]
    unbalanced = [r["entry_id"] for r in conn.execute(
        "SELECT entry_id FROM journal_lines GROUP BY entry_id "
        "HAVING SUM(debit_cents) <> SUM(credit_cents)")]
    # I4: a receipt movement carries the po_line id as its `ref`, so the join is
    # exact rather than heuristic. A line with no receipts yet must be 0, which
    # is why this is a LEFT JOIN over every line and not a join over movements.
    miscounted = [{"po_line_id": r["id"], "po_id": r["po_id"], "sku": r["sku"],
                   "qty_received": float(r["qty_received"]),
                   "movement_qty": float(r["mq"])}
                  for r in conn.execute(
                      "SELECT l.id, l.po_id, l.sku, l.qty_received, COALESCE(m.q, 0) AS mq "
                      "FROM po_lines l LEFT JOIN "
                      "(SELECT ref, SUM(qty) AS q FROM stock_movements "
                      "WHERE kind = 'receipt' GROUP BY ref) m ON m.ref = l.id")
                  if abs(float(r["qty_received"]) - float(r["mq"])) > EPS]
    ok = gl == moved and not drifted and not unbalanced and not miscounted
    return {"I1_gl_vs_movements": {"gl_inventory_cents": gl, "movement_value_cents": moved,
                                   "delta_cents": gl - moved, "ok": gl == moved},
            "I2_position_vs_movements": {"drifted": drifted, "ok": not drifted},
            "I3_entries_balanced": {"unbalanced_entries": unbalanced, "ok": not unbalanced},
            "I4_po_received_vs_movements": {"miscounted_lines": miscounted,
                                            "ok": not miscounted},
            "ok": ok}
