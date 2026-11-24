# Smoke shop DB

A small, self-contained SQLite database for a smoke shop: suppliers, a product
catalog with wholesale costs, inbound **deliveries**, lot/expiry tracking for
perishables, an inventory ledger, and a repeatable way to **generate new
deliveries** so the receiving workflow can be exercised end to end.

No dependencies — Python 3.11 standard library only (`sqlite3`, `random`,
`argparse`).

> This directory is a standalone project that happens to sit inside the nixpkgs
> checkout used for this session. Nothing here is wired into nixpkgs: no
> package, no module, no `default.nix` changes. Copy/move the `smoke-shop-db`
> folder out to its own repo when you're done with it.

## Quick start

```bash
cd smoke-shop-db

python3 run_deliveries.py --reset --count 6 --seed 42   # build DB + receive 6 deliveries
python3 report.py                                       # reports/inventory-YYYY-MM-DD.md
python3 -m unittest discover -s tests -v                # 17 tests
```

`smoke_shop.db` is created on first use and seeded automatically.

## Schema

| Table | Purpose |
| --- | --- |
| `suppliers` | Distributors, contact info, lead time |
| `products` | Catalog: SKU, brand, category, cost, retail, `min_stock`, `excise_bps`, `age_restricted` |
| `supplier_products` | Which supplier carries which SKU, and at what wholesale cost |
| `inventory` | Current `on_hand` per product |
| `deliveries` | One row per inbound shipment: number, supplier, invoice, received_at, status, total |
| `delivery_lines` | Line items: qty, unit cost, lot code, expiry, generated line total |
| `stock_movements` | Append-only ledger of every `on_hand` change |

Views: `v_inventory_status` (on hand vs. minimum, cost/retail value),
`v_delivery_summary` (per-delivery units, total, excise).

Design points worth knowing:

* **Money is integer cents** everywhere. No floats in the database or in the
  arithmetic.
* **Stock is double-entry.** Receiving writes both `inventory.on_hand` and a
  `stock_movements` row, so the balance can always be rebuilt from history.
  `report.py` asserts the two agree and flags any SKU where they diverge.
* **Invoices are idempotent by constraint.** `UNIQUE (supplier_id, invoice_number)`
  means the same invoice cannot be received — and therefore counted — twice.
* **Receiving is atomic.** A delivery and all of its lines are inserted in one
  transaction; a bad quantity or duplicate invoice rolls the whole thing back.
* **Totals are maintained by triggers**, so `deliveries.total_cents` stays
  correct even when lines are edited by hand.

## Generating deliveries

```bash
python3 run_deliveries.py --count 8              # 8 deliveries, random
python3 run_deliveries.py --count 8 --seed 42    # reproducible batch
python3 run_deliveries.py --count 3 --days 30    # spread over the last month
python3 run_deliveries.py --json | jq .          # machine-readable summary
python3 run_deliveries.py --reset                # drop everything and rebuild
```

What a generated delivery looks like:

* a supplier chosen by catalog size, so the big distributors appear more often
  than the niche glass importer;
* 3–7 distinct SKUs that supplier actually carries;
* case-size quantities by category (12/24/48/96 for disposables, 6-bottle
  steps for e-liquid, 2/4/8 for glass…);
* wholesale cost drifted ±5% from the catalog price (occasionally +6–9%);
* a hand-copyable lot code, e.g. `LOT-2611-K3M`;
* an expiry date 6–24 months out for perishable categories only
  (`disposable`, `e_liquid`, `nicotine_pouch`, `cbd_topical`);
* a unique invoice number and a sequential `DLV-<year>-<seq>` number
  (assigned at entry time, so a backdated invoice keeps the next number in line).

### Two-step receiving

`receive_delivery(..., status="pending")` records a delivery without touching
stock; `receive_pending(conn, id, at=...)` promotes it and applies stock exactly
once. Useful for deliveries that arrive on the dock before the invoice is
checked.

## Report

`python3 report.py` writes `reports/inventory-YYYY-MM-DD.md`: inventory
valuation at cost and retail, implied margin, reorder list with estimated
restock cost, expiry watch (default 90 days, `--expiry-days`), the most recent
deliveries with excise, and the ledger integrity check. `--stdout` prints it
instead of writing a file.

## Assumptions / deliberate simplifications

* Brands and suppliers are fictional; prices are plausible US retail, not a
  real price list.
* `age_restricted` is data only — it flags products that need 21+ handling, it
  does not enforce anything at the register.
* `excise_bps` is a simple basis-point rate on wholesale cost per product. Real
  nicotine excise varies by state, product type and unit count; keep the rates
  in `seed.sql` as placeholders to replace with your jurisdiction's rules.
* `min_stock` is a flat per-SKU minimum, not a demand-driven reorder point.
* Sales, shrink, and returns have ledger `reason` values reserved
  (`sale`, `return`, `damage`, `adjustment`) but no workflow yet.
