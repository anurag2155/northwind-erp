"""The route table: every route in the system is defined exactly once, here.

Ownership, so that adding a route has one obvious home:

    erp.py        business rules and the only SQL
    readmodel.py  read-only projections, no routes
    routes.py     the (method, path, roles, status, handler) table -- this file
    server.py     HTTP transport: auth, matching, RBAC enforcement, CORS

`GET /me` derives the caller's permitted routes from ROUTES, so the console
holds no second copy of the RBAC rules and cannot offer a permission the API
would refuse. The table is passed to `readmodel.me()` as an argument rather
than imported back from the server, so there is no import cycle.

Roles: a set of role names gates the route; `ANY` (and only `ANY`) opens it to
any authenticated caller. An empty set is rejected at import -- see AnyRole.
"""
import erp
import readmodel

class AnyRole(frozenset):
  """"Any authenticated caller", as a distinct type rather than a bare `set()`.

  A plain empty set must never mean public: that reading is what made a
  one-token typo publish the finance ledger. Only this type opens a route; an
  empty set is a configuration error, enforced at import and in dispatch.
  """
  __slots__ = ()


ANY = AnyRole()
PO_ROLES = {"buyer", "purchasing_manager", "warehouse_clerk"}
SO_ROLES = {"sales_rep", "warehouse_shipper"}

# (method, path, roles, success status, handler(conn, user, params, body))
ROUTES = [
    # -- writes: every one goes through erp.py inside a single transaction -----
    ("POST", "/purchase-orders", {"buyer"}, 201,
     lambda c, u, p, b: erp.Procurement.create_po(c, u, b)),
    ("POST", "/purchase-orders/{po_id}/approve", {"purchasing_manager"}, 200,
     lambda c, u, p, b: erp.Procurement.approve_po(c, u, p["po_id"])),
    ("POST", "/purchase-orders/{po_id}/receipts", {"warehouse_clerk"}, 201,
     lambda c, u, p, b: erp.Procurement.receive(c, u, p["po_id"], b)),
    ("POST", "/sales-orders", {"sales_rep"}, 201,
     lambda c, u, p, b: erp.Sales.create_so(c, u, b)),
    ("POST", "/sales-orders/{so_id}/confirm", {"sales_rep"}, 200,
     lambda c, u, p, b: erp.Sales.confirm_so(c, u, p["so_id"], b)),
    ("POST", "/sales-orders/{so_id}/shipments", {"warehouse_shipper"}, 201,
     lambda c, u, p, b: erp.Sales.ship_so(c, u, p["so_id"], b)),
    ("POST", "/inventory/transfers", {"warehouse_clerk"}, 201,
     lambda c, u, p, b: erp.Inventory.transfer(c, u, b)),
    ("POST", "/ledger/journals/{journal_id}/reverse", {"accountant"}, 201,
     lambda c, u, p, b: erp.Ledger.reverse(c, p["journal_id"], u["id"],
                                          (b or {}).get("reason"))),

    # -- reads: projections in readmodel.py, reconciliation in erp.Reports ----
    ("GET", "/me", ANY, 200,
     lambda c, u, p, b: readmodel.me(c, u, ROUTES)),
    ("GET", "/inventory/positions", ANY, 200,
     lambda c, u, p, b: readmodel.positions(c)),
    ("GET", "/inventory/movements", ANY, 200,
     lambda c, u, p, b: readmodel.movements(c, b or {})),
    ("GET", "/inventory/skus/{sku}/availability", ANY, 200,
     lambda c, u, p, b: erp.Inventory.snapshot(c, p["sku"],
                                               (b or {}).get("warehouse_id") or "WH-MAIN")),
    ("GET", "/purchase-orders", PO_ROLES, 200,
     lambda c, u, p, b: readmodel.purchase_orders(c)),
    ("GET", "/sales-orders", SO_ROLES, 200,
     lambda c, u, p, b: readmodel.sales_orders(c)),
    ("GET", "/ledger/journals", {"accountant"}, 200,
     lambda c, u, p, b: readmodel.journals(c, b or {})),
    ("GET", "/ledger/inventory-reconciliation", {"accountant"}, 200,
     lambda c, u, p, b: erp.Reports.reconcile(c)),
]


for _method, _path, _roles, _status, _handler in ROUTES:
  # Fail at import rather than quietly publishing a route.
  if not isinstance(_roles, AnyRole) and not _roles:
    raise RuntimeError("route %s %s has an empty role set; write ANY if it is "
                       "meant to be open to any authenticated caller"
                       % (_method, _path))
del _method, _path, _roles, _status, _handler
