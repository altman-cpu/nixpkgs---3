#!/usr/bin/env python3
"""Generate and receive new deliveries for the smoke shop DB.

Each run writes N new supplier deliveries (invoice, lines, lot codes and, for
perishables, expiry dates), applies them to inventory, and prints a receiving
summary plus the reorder list.

    python3 run_deliveries.py                        # 5 deliveries, random seed
    python3 run_deliveries.py --count 8 --seed 42    # reproducible run
    python3 run_deliveries.py --reset --count 3      # rebuild DB first
    python3 run_deliveries.py --json                 # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from db import (
    DEFAULT_DB,
    PERISHABLE,
    init_db,
    money,
    open_db,
    receive_delivery,
    totals,
)

# Units ordered per line, by category: (min, max, step) — wholesale case sizes.
QTY_RANGES = {
    "vape_device":    (6, 24, 6),
    "disposable":     (24, 96, 12),
    "e_liquid":       (12, 48, 6),
    "nicotine_pouch": (24, 120, 12),
    "glass":          (2, 12, 2),
    "accessory":      (6, 36, 6),
    "rolling_paper":  (24, 120, 12),
    "cbd_topical":    (4, 24, 4),
}

# Shelf life in days, by category (only categories in db.PERISHABLE).
SHELF_LIFE_DAYS = {
    "disposable":     (365, 540),
    "e_liquid":       (240, 540),
    "nicotine_pouch": (180, 365),
    "cbd_topical":    (365, 720),
}

LOT_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1: hand-copyable


def load_catalog(conn):
    rows = conn.execute(
        "SELECT sp.supplier_id, sp.unit_cost_cents, "
        "       p.id, p.sku, p.name, p.brand, p.category, p.excise_bps "
        "  FROM supplier_products sp JOIN products p ON p.id = sp.product_id"
    ).fetchall()
    catalog: dict[int, list] = {}
    for row in rows:
        catalog.setdefault(row["supplier_id"], []).append(dict(row))
    return catalog


def load_suppliers(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM suppliers ORDER BY id")]


def pick_supplier(rng: random.Random, suppliers: list[dict], catalog: dict) -> dict:
    """Weight suppliers by how many lines they carry, so the big distributors
    show up more often than the niche glass importer."""
    weights = [len(catalog[s["id"]]) for s in suppliers]
    return rng.choices(suppliers, weights=weights, k=1)[0]


def pick_qty(rng: random.Random, category: str) -> int:
    lo, hi, step = QTY_RANGES[category]
    return rng.randrange(lo, hi + 1, step)


def drift_cost(rng: random.Random, base_cents: int) -> int:
    """Simulate wholesale cost movement between shipments: mostly ±5%,
    occasionally a 6-9% bump. Always at least one cent."""
    if rng.random() < 0.12:
        pct = rng.uniform(0.06, 0.09)
    else:
        pct = rng.uniform(-0.05, 0.05)
    return max(1, round(base_cents * (1 + pct)))


def lot_code(rng: random.Random, received_at: datetime) -> str:
    suffix = "".join(rng.choice(LOT_ALPHABET) for _ in range(3))
    return f"LOT-{received_at:%y%m}-{suffix}"


def invoice_number(rng: random.Random, supplier: dict, used: set[str]) -> str:
    prefix = "".join(w[0] for w in supplier["name"].replace("&", "").split()).upper()[:3]
    for _ in range(100):
        candidate = f"INV-{prefix}-{rng.randint(1000, 9999)}"
        if candidate not in used:
            used.add(candidate)
            return candidate
    raise RuntimeError(f"could not allocate a unique invoice number for {supplier['name']}")


def build_delivery(rng: random.Random, received_at: datetime, supplier: dict,
                   catalog: dict, used_invoices: set[str]) -> dict:
    products = catalog[supplier["id"]]
    line_count = min(len(products), rng.choices([3, 4, 5, 6, 7], weights=[2, 4, 5, 3, 1])[0])
    picked = rng.sample(products, line_count)

    lines = []
    for p in sorted(picked, key=lambda x: x["sku"]):
        qty = pick_qty(rng, p["category"])
        unit_cost = drift_cost(rng, p["unit_cost_cents"])
        expires_on = None
        if p["category"] in PERISHABLE:
            lo, hi = SHELF_LIFE_DAYS[p["category"]]
            expires_on = (received_at + timedelta(days=rng.randint(lo, hi))).date().isoformat()
        lines.append(
            {
                "product_id": p["id"],
                "sku": p["sku"],
                "name": p["name"],
                "category": p["category"],
                "qty": qty,
                "unit_cost_cents": unit_cost,
                "excise_bps": p["excise_bps"],
                "lot_code": lot_code(rng, received_at),
                "expires_on": expires_on,
            }
        )

    return {
        "supplier": supplier,
        "received_at": received_at,
        "invoice_number": invoice_number(rng, supplier, used_invoices),
        "lines": lines,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="run_deliveries.py",
                                    description="Generate and receive new smoke shop deliveries.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite database path")
    ap.add_argument("--count", type=int, default=5, help="number of deliveries to create (default 5)")
    ap.add_argument("--seed", type=int, default=None, help="random seed for a reproducible run")
    ap.add_argument("--days", type=int, default=10, help="spread deliveries over the last N days (default 10)")
    ap.add_argument("--reset", action="store_true", help="drop and rebuild the database first")
    ap.add_argument("--json", action="store_true", help="print a JSON summary instead of a table")
    args = ap.parse_args(argv)

    if args.count < 1:
        ap.error("--count must be at least 1")
    if args.days < 1:
        ap.error("--days must be at least 1")

    rng = random.Random(args.seed)
    conn = init_db(args.db, reset=True) if args.reset else open_db(args.db)
    suppliers = load_suppliers(conn)
    catalog = load_catalog(conn)
    if not suppliers or not catalog:
        print("database has no suppliers or catalog; run with --reset", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc).replace(microsecond=0)
    used_invoices = {
        r["invoice_number"] for r in conn.execute("SELECT invoice_number FROM deliveries")
    }

    # Build the whole batch first, in chronological order, so delivery numbers
    # are ascending and no partial batch is written if generation fails.
    planned = []
    for _ in range(args.count):
        offset = rng.randint(0, args.days - 1)
        received_at = (now - timedelta(days=offset)).replace(
            hour=rng.randint(9, 17), minute=rng.choice([0, 7, 15, 23, 38, 47, 52])
        )
        supplier = pick_supplier(rng, suppliers, catalog)
        planned.append(build_delivery(rng, received_at, supplier, catalog, used_invoices))
    planned.sort(key=lambda d: d["received_at"])

    created = []
    for batch in planned:
        delivery_id = receive_delivery(
            conn,
            supplier_id=batch["supplier"]["id"],
            invoice_number=batch["invoice_number"],
            received_at=batch["received_at"].isoformat().replace("+00:00", "Z"),
            lines=batch["lines"],
        )
        row = conn.execute(
            "SELECT * FROM v_delivery_summary WHERE id = ?", (delivery_id,)
        ).fetchone()
        created.append({**batch, "id": delivery_id, "summary": dict(row)})

    result = {
        "db": str(args.db),
        "seed": args.seed,
        "count": len(created),
        "totals": totals(conn),
        "deliveries": [
            {
                "delivery_number": c["summary"]["delivery_number"],
                "received_at": c["summary"]["received_at"],
                "supplier": c["supplier"]["name"],
                "invoice_number": c["invoice_number"],
                "units": c["summary"]["units"],
                "total_cents": c["summary"]["total_cents"],
                "excise_cents": c["summary"]["excise_cents"],
                "lines": [
                    {
                        "sku": line["sku"],
                        "name": line["name"],
                        "category": line["category"],
                        "qty": line["qty"],
                        "unit_cost_cents": line["unit_cost_cents"],
                        "line_total_cents": line["qty"] * line["unit_cost_cents"],
                        "lot_code": line["lot_code"],
                        "expires_on": line["expires_on"],
                    }
                    for line in c["lines"]
                ],
            }
            for c in created
        ],
        "low_stock": [
            {
                "sku": r["sku"],
                "name": r["name"],
                "on_hand": r["on_hand"],
                "min_stock": r["min_stock"],
            }
            for r in conn.execute(
                "SELECT * FROM v_inventory_status WHERE below_min ORDER BY category, sku"
            ).fetchall()
        ],
    }
    conn.close()

    if args.json:
        print(json.dumps(result, indent=2))
        return 0

    print_receiving_slip(result)
    return 0


def print_receiving_slip(result: dict) -> None:
    t = result["totals"]
    print(f"\nSmoke shop receiving — {result['db']}")
    print(
        f"{result['count']} new deliveries · {t['deliveries']} total · "
        f"{sum(d['units'] for d in result['deliveries']):,} units received"
    )
    print(
        f"Wholesale this run: {money(sum(d['total_cents'] for d in result['deliveries']))}"
        f"   excise accrued: {money(sum(d['excise_cents'] for d in result['deliveries']))}"
    )
    print("=" * 88)

    sup_width = max(len(d["supplier"]) for d in result["deliveries"])
    for d in result["deliveries"]:
        print(
            f"{d['delivery_number']}  {d['received_at'][:10]}  "
            f"{d['supplier']:<{sup_width}}  {d['invoice_number']}"
        )
        for line in d["lines"]:
            expiry = f"  exp {line['expires_on']}" if line["expires_on"] else ""
            print(
                f"   {line['sku']:<9} {line['name'][:30]:<30} {line['qty']:>4} × "
                f"{money(line['unit_cost_cents']):>7} = {money(line['line_total_cents']):>9}"
                f"  {line['lot_code']}{expiry}"
            )
        print(
            f"   {'':<9} {'— ' + str(len(d['lines'])) + ' lines':<30} {d['units']:>4} units"
            f"{'':>10}   {money(d['total_cents']):>10}  excise {money(d['excise_cents'])}"
        )
        print("-" * 88)

    low = result["low_stock"]
    if low:
        print(f"Reorder — {len(low)} SKU(s) below minimum:")
        for r in low[:8]:
            shortfall = r["min_stock"] - r["on_hand"]
            print(
                f"   {r['sku']:<9} {r['name'][:34]:<34} on hand {r['on_hand']:>4} "
                f"/ min {r['min_stock']:>3}  (order {shortfall})"
            )
        if len(low) > 8:
            print(f"   … and {len(low) - 8} more")
    else:
        print("Reorder — every SKU is at or above its minimum.")
    print(f"\nFull report:  python3 report.py --db {result['db']}\n")


if __name__ == "__main__":
    raise SystemExit(main())
