# Northwind Mini-ERP

Inventory, procurement, sales fulfilment and a double-entry ledger, built across
four stages. This document is written for whoever picks it up next: what it is,
how to run it, how it is deployed, and the two mechanisms that actually matter —
how concurrent orders cannot oversell stock, and how the ledger stays consistent
with the stock it accounts for.

## Live

| | URL |
|---|---|
| API | `<BACKEND_URL>` — `GET /` and `/health` are unauthenticated |
| Console | `<FRONTEND_URL>` |

Sign in with a demo token: `demo-kabir` (warehouse clerk), `demo-nina` (sales),
`demo-tara` (accountant), `demo-ravi` (buyer), `demo-meera` (purchasing manager),
`demo-omar` (shipper). Each role sees a different console — that is the point of
the RBAC section below, and it is visible in about ten seconds.

## Run it locally

```bash
docker compose up --build
```

That is the whole thing. It builds both images, seeds the database on boot, waits
for the API's health check before starting the console, and leaves you at
<http://localhost:5173/console.html> talking to <http://localhost:8080>. Sign in
with any token above. No `.env` to write, no migration to run, no seed script to
remember — seeding happens in `server.reset_database()` at startup.

Without Docker, the same thing in two terminals:

```bash
cd backend  && ERP_DEMO=1 python3 server.py          # API on :8080
cd frontend && python3 -m http.server 5173           # console on :5173
```

Python 3.10+ and the standard library only. There are no dependencies to install
in either service, which is why there is no lockfile to go stale.

Nothing is fixed to those port numbers. If 8080 or 5173 is taken on your machine,
run `PORT=8090 ERP_DEMO=1 python3 server.py` and open the console with
`?api=http://127.0.0.1:8090`; for Compose, change the left-hand side of the
`ports:` mapping. The backend prints exactly this if the bind fails.

To point the console at a different API without rebuilding:
`console.html?api=https://your-api.example.com`.

## Architecture

Four stages, four artefacts, one system:

```
frontend/                      backend/
  console.html  shell            server.py     transport: auth, routing, RBAC, CORS
  config.js     API base         readmodel.py  read-only projections for the console
  api.js        transport        erp.py        schema + every business rule
  store.js      state                            Ledger / Inventory / Procurement
  views.js      pure render                      / Sales / Reports
  app.js        DOM wiring       regression.py Stage 4 bug regressions
                                 proofs.py     Stage 2 invariant proofs
```

Two rules hold the shape together.

**Only `erp.py` touches the database, and only `Inventory` writes stock.** Every
movement of stock or value goes through `Inventory._commit`, so there is exactly
one place where a position changes. `server.py` and `readmodel.py` move bytes and
decide nothing.

**Rendering is a pure function.** `views.js` is `render(state) -> HTML string`
with no DOM, no `fetch`, no globals. That is not aesthetics: it is why a browser
UI in this repo has reproducible evidence. `evidence.mjs` boots the real backend,
drives the same `api`/`store` modules the browser mounts, renders the same view
functions, and prints the result.

The database is SQLite, embedded in the API container. That is a deliberate scope
cut with real consequences — see Limitations and the 100x section.

### RBAC is defined once

Roles live in the route table in `server.py`, and `GET /me` returns the caller's
permitted routes derived from *that same table*. The console gates every tab and
button on that response, so it holds no second copy of the rules. Change a role
server-side and the UI follows with no frontend edit; the console cannot offer a
permission the API would refuse.

## Concurrency: how two orders cannot both take the last unit

The mechanism is **`BEGIN IMMEDIATE` on every write transaction**, and the
important part is *where the availability read happens*, not what guards the
write.

`in_transaction()` opens each write with `BEGIN IMMEDIATE`, which takes SQLite's
writer lock at BEGIN rather than at first write. `Sales.confirm_so` then reads
the position **inside** that transaction via `Inventory.reserve`, computes
`available = on_hand - reserved`, and writes. Because the lock is already held
when the read happens, two concurrent confirms cannot both observe the same
pre-image. The loser sees the winner's reservation and gets `409
INSUFFICIENT_STOCK` — a business answer, not a crash.

`proofs.py` fires 20 concurrent confirms at a single unit of stock and asserts
exactly one winner (`regression.py` repeats it with 8). Both print the server's
own **peak in-flight request counter**, because a sequential run would produce
the same 1-winner result and prove nothing:

```
20 concurrent confirms vs 1 unit -> 200x1 409x19 | peak in-flight 20 (overlapped)
```

Two further guards exist, and Stage 4 measured what each is actually worth by
breaking the ordering and restoring one at a time:

| guard restored alone | result |
|---|---|
| version CAS on `positions` | **still oversells** — 8 winners for 1 unit |
| `CHECK (reserved <= on_hand)` | 1 winner, but 7 × `409 INTEGRITY_VIOLATION` |
| `BEGIN IMMEDIATE` | 1 winner, 7 × `409 INSUFFICIENT_STOCK` |

Worth internalising before changing this code. The **CAS does not help** here:
optimistic concurrency protects the row it versioned, and each transaction's own
read-modify-write is internally consistent — the stale value was the *decision*,
taken from a read the CAS never saw. It stays in the code because it is what
ports the guard to Postgres, where readers are not blocked. The **CHECK** does
stop the oversell, but by having the database refuse the write, so the client
gets a constraint leak instead of an answer. It stays as a backstop that makes
overselling unrepresentable. Neither is the fix; the ordering is.

## Ledger consistency: what guarantees it, and what it does not cover

**Every stock movement carries a journal entry.** `stock_movements.journal_id` is
`NOT NULL` with a foreign key, unconditionally. That is only possible because
reservations are deliberately *not* movements — reserving stock moves no value,
so it never needs an entry. If value moved, the ledger moved with it.

**The ledger is append-only.** Triggers reject `UPDATE` **and** `DELETE` on
`journal_entries`, `journal_lines` and `stock_movements`. A ledger you can delete
from is not immutable, only inconvenient to edit. Corrections go through
`Ledger.reverse`, which posts a new entry with debits and credits swapped, and a
partial unique index on `reverses_id` allows exactly one reversal per entry — a
second attempt is a `409`, not a double credit.

**Value is removed proportionally, not at a recomputed unit cost.** When stock is
issued, `int(round(value_cents * qty / on_hand))` is both the cents removed from
the position and the cents credited to inventory in the ledger. They are the same
integer, so reconciliation is exact by construction rather than exact within a
tolerance. Concretely: receive 10 @ 250c and 4 @ 400c (4100c over 14 units), ship
6, and COGS 1757c + remaining inventory 2343c = 4100c with no cent lost.

**`GET /ledger/inventory-reconciliation` reports three invariants separately**,
because one boolean would hide which broke:

- **I1** — GL account 1300 equals the summed value of all stock movements.
- **I2** — each materialised `positions` row equals the movements that produced
  it. `positions` is a denormalisation, and this is what keeps it honest.
- **I3** — every journal entry balances.

A stock transfer between warehouses is a linked **pair** of movements sharing one
journal entry — an issue at the source, a receipt at the destination, equal and
opposite — never an edit to a balance. The entry carries no lines on purpose:
inventory is a single GL account across warehouses, so a transfer relocates value
without revaluing it. A round-trip test asserts GL 1300 is byte-identical before
and after.

**What this does not cover, stated plainly.** The invariants tie the *general
ledger* to the *stock* sub-ledger. Nothing ties `po_lines.qty_received` to
`SUM(gr_lines.qty)`. A Stage 4 seeded bug that double-counted a second partial
receipt corrupted the purchase-order ledger while I1, I2 and I3 all stayed green,
because movements are driven by the received quantity, not by the column. An
"I4" over that pair is the first invariant I would add.

## How it is deployed

Render, from this repository, via `render.yaml` as a Blueprint:

1. Push this repo to GitHub.
2. Render dashboard → **New → Blueprint** → pick the repo → **Apply**.
3. Render reads `render.yaml` and creates two free-tier services:
   - **northwind-erp-api** — Docker, `backend/Dockerfile`, health check on
     `/health`, `HOST=0.0.0.0` so it binds where the platform expects and
     `PORT` injected by Render.
   - **northwind-erp-console** — static site from `frontend/`, whose build step
     rewrites `config.js` with the API's live hostname, so the console ships
     already pointing at its own backend.

Three things had to change for a platform deploy, all small and all in the repo:
binding `0.0.0.0:$PORT` instead of `127.0.0.1:8080`; an unauthenticated `GET /`
and `/health`, because a health probe and a reviewer both arrive before they have
a token; and CORS with a `204 OPTIONS` preflight, since the console is served
from a different origin and sends `Authorization` on POSTs.

## Verification

```bash
cd backend && python3 proofs.py                      # Stage 2 invariants, 7 proofs
cd backend && python3 regression.py erp.py readmodel.py FIXED   # Stage 4 regressions
cd verify  && node evidence.mjs                      # console, headless, 8 checks
```

All three pass on the deployed code. The regression suite is the interesting one:
each seeded bug build fails exactly its own test and passes the others, so the
tests are demonstrably specific rather than incidentally green.

## Limitations and scope cuts

- **SQLite, single writer.** Every write serialises on one database-level lock.
  Correct, and the reason the concurrency guarantee is so easy to state — but it
  is also the first thing to break under load (below).
- **The free tier has no persistent disk**, so the deployed database is
  ephemeral: it reseeds on restart or redeploy. Locally, `docker compose` mounts
  a named volume, so data survives a restart there. Nothing in the system depends
  on persistence being real, which is exactly why this is a demo and not a
  system of record.
- **`ERP_DEMO=1` makes the six tokens predictable** (`demo-ravi`, …) so the
  public console is usable. There is no login endpoint and no password. This is
  a demo affordance; a real deployment needs a session exchange and must not set
  this flag.
- **Quantities are floats** with epsilon comparisons. Money is integer cents and
  is exact; quantities would be better as a fixed-point integer.
- **Positional `INSERT ... VALUES (?,?,…)`** in several places. A Stage 4 column
  reorder broke one of these, which is the argument for named columns.
- **`Inventory.issue` conflates two ideas** — "stock leaves this position" and "a
  reservation is being fulfilled" — so stock transfers could not reuse it and
  needed a parallel path. Extracting a private `_move()` with `issue` layered on
  top is the right refactor.
- **Console scope**: forms hard-code `WH-MAIN` though the API is multi-warehouse;
  movement history is capped at 25 rows with no paging; a sales order is created
  and confirmed in one action, because a draft reserves nothing.
- **Transfer concurrency is argued, not proved.** The reservation race has a real
  concurrent test; transfers have edge-case and round-trip tests but no
  concurrent one.

## If this system had 100× the order volume

**The first thing to break is the single writer**, and it breaks before anything
else because it is load-bearing by design. `BEGIN IMMEDIATE` takes a
*database-level* lock: at 100× volume, a goods receipt in Bristol serialises
behind a sales confirmation in Leeds even though they touch different SKUs in
different warehouses. Throughput stops scaling with cores and becomes one
transaction at a time; `busy_timeout` absorbs the queue until it does not, and
then writes start failing with lock timeouts rather than business errors. Reads
degrade too, since the reconciliation report scans every movement.

**The fix, in the order I would do it.**

First, move to Postgres and replace the global writer lock with row-level
locking. The code is already shaped for this: `Inventory._commit` carries a
version compare-and-swap that is inert under SQLite and becomes the concurrency
control on Postgres, and `Inventory.transfer` already touches positions in a
deterministic `sorted()` order — the marked place where `SELECT … FOR UPDATE`
goes. Contention then scales with *contended SKUs* rather than with total write
volume, which is the actual shape of the load. The schema is portable apart from
the SQLite-specific trigger syntax for immutability, which becomes a `BEFORE
UPDATE OR DELETE` rule or a revoked table grant.

Second, expect the bottleneck to move to a single hot row: one popular SKU in one
warehouse still serialises every order for it. The answer is not a bigger lock
but a smaller one — split the position into N bucket rows per (sku, warehouse)
and reserve against a bucket, summing for availability. That trades an exact
instantaneous count for throughput, which is the correct trade for reservations
and the wrong one for the ledger, so only the position is bucketed.

Third, the reconciliation report becomes a table scan over an unbounded,
append-only movement table. It needs a periodic snapshot — a closed-period
balance per (sku, warehouse) — with the live report checking only movements since
the last close. That also gives month-end close, which a real ERP needs anyway.

What I would *not* do is split the modules into services. The invariant that a
stock movement and its balanced journal entry are written together is a single
`BEGIN`/`COMMIT` today; across services it becomes a distributed transaction or a
saga, which means the ledger is temporarily wrong by design and someone has to
decide how wrong is acceptable. At 100× the volume that is still the wrong
trade — one database that can take the write rate is cheaper than a correctness
argument nobody can hold in their head.
