"""Stage 3 read model: read-only projections the console needs; no business rule
lives here. `GET /me` is the important one -- it derives the caller's permitted
routes from server.ROUTES, so the UI keeps no second copy of the RBAC table.

Wired by one line in server.py: `import readmodel; ROUTES += readmodel.ROUTES`
"""
ANY = set()                    # authenticated, not role-gated
PO_ROLES = {"buyer", "purchasing_manager", "warehouse_clerk"}
SO_ROLES = {"sales_rep", "warehouse_shipper"}


def _rows(conn, sql, *args):
  return [dict(r) for r in conn.execute(sql, args)]


def _attach(parents, lines, fk, project):
  """Group child rows onto their parent under `lines`."""
  grouped = {}
  for l in lines:
    grouped.setdefault(l[fk], []).append(project(l))
  for p in parents:
    p["lines"] = grouped.get(p["id"], [])
  return parents


def me(conn, user):
  """Identity plus the routes this role may call, off the live route table."""
  import server
  allowed = [{"method": m, "path": p} for m, p, roles, _s, _h in server.ROUTES
             if not roles or user["role"] in roles]
  return {"id": user["id"], "role": user["role"], "permissions": allowed}


def positions(conn):
  def row(r):
    on_hand, value = float(r["on_hand"]), int(r["value_cents"])
    return {"sku": r["sku"], "warehouse_id": r["warehouse_id"], "on_hand": on_hand,
            "reserved": float(r["reserved"]), "available": on_hand - float(r["reserved"]),
            "value_cents": value,
            "unit_cost_cents": int(value / on_hand) if on_hand else 0}
  return {"positions": [row(r) for r in
                        _rows(conn, "SELECT * FROM positions ORDER BY sku, warehouse_id")]}


def movements(conn, query):
  """Newest first by rowid: the append order is the only ordering the movement
  table carries, as posted_at lives on the journal."""
  sql = ("SELECT m.rowid AS seq, m.sku, m.warehouse_id, m.kind, m.qty, m.value_cents, "
         "m.journal_id, m.ref, j.source, j.posted_by, j.posted_at "
         "FROM stock_movements m JOIN journal_entries j ON j.id = m.journal_id")
  where, args = [], []
  if query.get("sku"):
    where.append("m.sku = ?"); args.append(query["sku"])
  if query.get("warehouse_id"):
    where.append("m.warehouse_id = ?"); args.append(query["warehouse_id"])
  if where:
    sql += " WHERE " + " AND ".join(where)
  sql += " ORDER BY m.rowid DESC LIMIT ?"
  args.append(min(int(query.get("limit", 100)), 500))
  return {"movements": _rows(conn, sql, *args)}


def purchase_orders(conn):
  """Outstanding is per line, clamped at zero to match Procurement.receive."""
  def line(l):
    ordered, received = float(l["qty_ordered"]), float(l["qty_received"])
    return {"id": l["id"], "sku": l["sku"], "qty_ordered": ordered, "qty_received": received,
            "qty_outstanding": max(ordered - received, 0.0),
            "unit_cost_cents": int(l["unit_cost_cents"])}
  return {"purchase_orders": _attach(
      _rows(conn, "SELECT * FROM purchase_orders ORDER BY rowid DESC"),
      _rows(conn, "SELECT * FROM po_lines ORDER BY rowid"), "po_id", line)}


def sales_orders(conn):
  def line(l):
    return {"id": l["id"], "sku": l["sku"], "qty_ordered": float(l["qty_ordered"]),
            "qty_reserved": float(l["qty_reserved"]), "qty_shipped": float(l["qty_shipped"]),
            "qty_backordered": float(l["qty_backordered"]),
            "unit_price_cents": int(l["unit_price_cents"])}
  return {"sales_orders": _attach(
      _rows(conn, "SELECT * FROM sales_orders ORDER BY rowid DESC"),
      _rows(conn, "SELECT * FROM so_lines ORDER BY rowid"), "so_id", line)}


def journals(conn, query):
  entries = _attach(
      _rows(conn, "SELECT * FROM journal_entries ORDER BY rowid DESC LIMIT ?",
            min(int(query.get("limit", 50)), 200)),
      _rows(conn, "SELECT * FROM journal_lines ORDER BY rowid"), "entry_id",
      lambda l: {"account": l["account"], "debit_cents": int(l["debit_cents"]),
                 "credit_cents": int(l["credit_cents"])})
  for e in entries:
    e["total_cents"] = sum(x["debit_cents"] for x in e["lines"])
  return {"journals": entries}


ROUTES = [
    ("GET", "/me", ANY, 200, lambda c, u, p, b: me(c, u)),
    ("GET", "/inventory/positions", ANY, 200, lambda c, u, p, b: positions(c)),
    ("GET", "/inventory/movements", ANY, 200, lambda c, u, p, b: movements(c, b or {})),
    ("GET", "/purchase-orders", PO_ROLES, 200, lambda c, u, p, b: purchase_orders(c)),
    ("GET", "/sales-orders", SO_ROLES, 200, lambda c, u, p, b: sales_orders(c)),
    ("GET", "/ledger/journals", {"accountant"}, 200, lambda c, u, p, b: journals(c, b or {}))]
