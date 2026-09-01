"""The route table. Every route in the system is defined exactly once, here.

Stage 4 flagged that adding the transfer route meant choosing between two files
with no rule saying which: `server.py` held the write routes and `readmodel.py`
appended the read ones, and `readmodel.me()` then had to `import server` lazily
*inside the function* to read the live table back -- a circular import papered
over with an import statement in an odd place.

Splitting the table was the cause of all of that, so the table moved out of both
modules into this one. What each module owns is now stateable in a sentence:

    erp.py        business rules and the only SQL
    readmodel.py  read-only projections, no routes
    routes.py     the (method, path, roles, status, handler) table -- this file
    server.py     HTTP transport: auth, matching, RBAC enforcement, CORS

`GET /me` returns the caller's permitted routes off ROUTES itself, so the
console holds no second copy of the RBAC rules and cannot offer a permission
the API would refuse. It reads the table by argument now rather than by
importing the server, which is why the circularity is gone rather than hidden.

Roles semantics, unchanged and load-bearing: an EMPTY role set means "any
authenticated caller". Stage 4's Bug 3 was a route that got `ANY` by a
copy-paste when it should have been `{"accountant"}` -- one token, and the
finance ledger was world-readable to anyone with a token. `ANY` is spelled out
as a named constant precisely so it reads as a deliberate choice.
"""
import erp
import readmodel

ANY = set()                              # authenticated, but not role-gated
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
