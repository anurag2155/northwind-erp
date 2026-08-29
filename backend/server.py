#!/usr/bin/env python3
"""Transport for erp.py: auth, routing, RBAC, HTTP, seeding. No business rules."""
import json, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import erp
from erp import ApiError

SEED_ROLES = [("ravi", "buyer"), ("meera", "purchasing_manager"), ("kabir", "warehouse_clerk"),
              ("nina", "sales_rep"), ("omar", "warehouse_shipper"), ("tara", "accountant")]

def reset_database():
  """Rebuild; tokens are random, not derived from user ids."""
  import os, secrets
  if os.path.exists(erp.DB_PATH):
    os.remove(erp.DB_PATH)
  conn = erp.connect()
  try:
    conn.executescript(erp.SCHEMA)
    tokens = {}
    for user_id, role in SEED_ROLES:
      tokens[user_id] = ("demo-" + user_id if os.environ.get("ERP_DEMO") == "1"
                         else secrets.token_hex(16))
      conn.execute("INSERT INTO users VALUES (?,?)", (user_id, role))
      conn.execute("INSERT INTO sessions VALUES (?,?)", (tokens[user_id], user_id))
    conn.execute("INSERT INTO warehouses VALUES ('WH-MAIN')")
    conn.execute("INSERT INTO warehouses VALUES ('WH-WEST')")
    conn.execute("INSERT INTO products VALUES ('WIDGET', 'Widget')")
    conn.execute("INSERT INTO products VALUES ('GIZMO', 'Gizmo')")
    return tokens
  finally:
    conn.close()

# (method, path, roles, status, handler(conn, user, params, body)).
ROUTES = [
    ("POST", "/purchase-orders", {"buyer"}, 201, lambda c,u,p,b: erp.Procurement.create_po(c, u, b)),
    ("POST", "/purchase-orders/{po_id}/approve", {"purchasing_manager"}, 200,
     lambda c,u,p,b: erp.Procurement.approve_po(c, u, p["po_id"])),
    ("POST", "/purchase-orders/{po_id}/receipts", {"warehouse_clerk"}, 201,
     lambda c,u,p,b: erp.Procurement.receive(c, u, p["po_id"], b)),
    ("POST", "/sales-orders", {"sales_rep"}, 201, lambda c,u,p,b: erp.Sales.create_so(c, u, b)),
    ("POST", "/sales-orders/{so_id}/confirm", {"sales_rep"}, 200,
     lambda c,u,p,b: erp.Sales.confirm_so(c, u, p["so_id"], b)),
    ("POST", "/sales-orders/{so_id}/shipments", {"warehouse_shipper"}, 201,
     lambda c,u,p,b: erp.Sales.ship_so(c, u, p["so_id"], b)),
    ("POST", "/ledger/journals/{journal_id}/reverse", {"accountant"}, 201,
     lambda c,u,p,b: erp.Ledger.reverse(c, p["journal_id"], u["id"], (b or {}).get("reason"))),
    ("GET", "/ledger/inventory-reconciliation", {"accountant"}, 200,
     lambda c,u,p,b: erp.Reports.reconcile(c)),
    ("POST", "/inventory/transfers", {"warehouse_clerk"}, 201,
     lambda c,u,p,b: erp.Inventory.transfer(c, u, b)),
    ("GET", "/inventory/skus/{sku}/availability", set(), 200,
     lambda c,u,p,b: erp.Inventory.snapshot(c, p["sku"], (b or {}).get("warehouse_id") or "WH-MAIN")),
]

import readmodel                      # Stage 3 read-only projections
ROUTES += readmodel.ROUTES


def _match(spec, segments):
  spec = [s for s in spec.split("/") if s]
  if len(spec) != len(segments):
    return None
  params = {}
  for want, got in zip(spec, segments):
    if want.startswith("{"):
      params[want[1:-1]] = got
    elif want != got:
      return None
  return params

def authenticate(header):
  if not header or not header.startswith("Bearer "):
    raise ApiError(401, "MISSING_BEARER_TOKEN")
  conn = erp.connect()
  try:
    row = erp.one(conn, "SELECT u.id, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
                        "WHERE s.token = ?", header.split(" ", 1)[1].strip())
  finally:
    conn.close()
  if row is None:
    raise ApiError(401, "INVALID_TOKEN")
  return {"id": row["id"], "role": row["role"]}

def dispatch(user, method, path, body):   # unknown path 404; wrong role 403
  segments = [s for s in path.split("/") if s]
  for r_method, spec, roles, status, handler in ROUTES:
    if r_method != method:
      continue
    params = _match(spec, segments)
    if params is None:
      continue
    if roles and user["role"] not in roles:
      raise ApiError(403, "FORBIDDEN", role=user["role"], required=sorted(roles))
    if method == "GET":
      conn = erp.connect()
      try:
        return status, handler(conn, user, params, body)
      finally:
        conn.close()
    return status, erp.in_transaction(lambda c: handler(c, user, params, body))
  raise ApiError(404, "NO_SUCH_ROUTE", method=method, path=path)


# Concurrency evidence: had requests run one after another, peak would be 1.
IN_FLIGHT = {"now": 0, "peak": 0}
_flight_lock = threading.Lock()

class Handler(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"

  def log_message(self, *args):
    pass

  # Stage 3: the console is served from a different origin (a static server on
  # :5173), so the API answers preflight and echoes the caller's origin. Cheaper
  # and more honest than teaching the API to serve HTML.
  CORS = {"Access-Control-Allow-Origin": "*",
          "Access-Control-Allow-Headers": "Authorization, Content-Type",
          "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}

  def _respond(self, status, payload):
    blob = json.dumps(payload).encode()
    self.send_response(status)
    self.send_header("Content-Type", "application/json")
    self.send_header("Content-Length", str(len(blob)))
    for k, v in self.CORS.items():
      self.send_header(k, v)
    self.end_headers()
    self.wfile.write(blob)

  def do_OPTIONS(self):
    self.send_response(204)
    for k, v in self.CORS.items():
      self.send_header(k, v)
    self.send_header("Content-Length", "0")
    self.end_headers()

  def _serve(self, method):
    with _flight_lock:
      IN_FLIGHT["now"] += 1
      IN_FLIGHT["peak"] = max(IN_FLIGHT["peak"], IN_FLIGHT["now"])
    try:
      path, _, query = self.path.partition("?")
      if path in ("/", "/health") and method == "GET":
        conn = erp.connect()
        try:
          skus = erp.one(conn, "SELECT COUNT(*) AS n FROM products")["n"]
        finally:
          conn.close()
        return self._respond(200, {"service": "northwind-erp", "status": "ok",
                                   "stage": "4-stage capstone", "products": skus,
                                   "docs": "see README", "auth": "Bearer token required"})
      body = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
      if method == "POST":
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
          body = json.loads(raw) if raw else {}
        except ValueError:
          raise ApiError(400, "INVALID_JSON")
      user = authenticate(self.headers.get("Authorization"))
      status, payload = dispatch(user, method, path, body)
      self._respond(status, payload)
    except ApiError as err:
      self._respond(err.status, err.body)
    except erp.sqlite3.IntegrityError as err:   # classified by type, not message text
      self._respond(409, {"error": "INTEGRITY_VIOLATION", "constraint": str(err)})
    except KeyError as err:
      self._respond(400, {"error": "MISSING_FIELD", "field": str(err)})
    except (TypeError, ValueError) as err:
      self._respond(400, {"error": "BAD_FIELD", "detail": str(err)})
    except Exception as err:                    # never leak a stack trace
      self._respond(500, {"error": "INTERNAL", "kind": type(err).__name__})
    finally:
      with _flight_lock:
        IN_FLIGHT["now"] -= 1

  def do_GET(self):
    self._serve("GET")

  def do_POST(self):
    self._serve("POST")

class Server(ThreadingHTTPServer):
  # Default backlog is 5; the concurrency proof opens 20 sockets at once, and
  # without this the surplus are reset before a handler sees them -- which would
  # read as a lost race rather than a refused connection.
  request_queue_size = 128
  daemon_threads = True
  allow_reuse_address = True
  block_on_close = False

def serve(port=0, host="127.0.0.1"):
  IN_FLIGHT["now"] = IN_FLIGHT["peak"] = 0
  httpd = Server((host, port), Handler)
  threading.Thread(target=httpd.serve_forever, daemon=True).start()
  return httpd, httpd.server_address[1]


if __name__ == "__main__":
  import os
  # A PaaS hands us the port and expects 0.0.0.0; locally these default to the
  # values the Stage 3 console already points at.
  port = int(os.environ.get("PORT", "8080"))
  host = os.environ.get("HOST", "127.0.0.1")
  creds = reset_database()
  httpd, port = serve(port, host)
  print("mini-erp listening on http://%s:%d" % (host, port))
  for name, token in creds.items():
    print("  %-8s %s" % (name, token))
  try:
    threading.Event().wait()
  except KeyboardInterrupt:
    httpd.shutdown()
