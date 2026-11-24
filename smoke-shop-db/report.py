#!/usr/bin/env python3
"""Inventory + receiving report for the smoke shop DB.

    python3 report.py                         # writes reports/inventory-YYYY-MM-DD.md
    python3 report.py --stdout                # print the markdown instead
    python3 report.py --expiry-days 60 --db other.db
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from pathlib import Path

from db import DEFAULT_DB, inventory_drift, low_stock, money, open_db, totals


def pct(part: int, whole: int) -> str:
    return f"{(100 * part / whole):.1f}%" if whole else "—"


def build_report(conn, expiry_days: int) -> str:
    t = totals(conn)
    val = conn.execute(
        "SELECT COALESCE(SUM(cost_value_cents), 0)   AS cost_cents, "
        "       COALESCE(SUM(retail_value_cents), 0) AS retail_cents, "
        "       COUNT(*)                            AS skus "
        "  FROM v_inventory_status"
    ).fetchone()
    spend = conn.execute(
        "SELECT COALESCE(SUM(total_cents), 0) AS spend, "
        "       COALESCE(SUM(excise_cents), 0) AS excise, "
        "       COALESCE(SUM(units), 0)        AS units "
        "  FROM v_delivery_summary WHERE status <> 'void'"
    ).fetchone()

    lines = [
        f"# Smoke shop inventory report — {date.today().isoformat()}",
        "",
        "_Generated from the SQLite ledger; money is wholesale/retail in USD._",
        "",
        "## Snapshot",
        "",
        "| Metric | Value |",
        "| --- | --- |",
        f"| SKUs on file | {val['skus']} |",
        f"| Units on hand | {t['units_on_hand']:,} |",
        f"| Inventory at cost | {money(val['cost_cents'])} |",
        f"| Inventory at retail | {money(val['retail_cents'])} |",
        f"| Implied margin | {pct(val['retail_cents'] - val['cost_cents'], val['retail_cents'])} |",
        f"| Deliveries received | {t['deliveries']} ({spend['units']:,} units) |",
        f"| Wholesale spend | {money(spend['spend'])} |",
        f"| Excise accrued | {money(spend['excise'])} |",
        f"| SKUs below minimum | {t['below_min']} |",
        "",
        "## Reorder",
        "",
    ]

    low = low_stock(conn)
    if low:
        lines += [
            "| SKU | Product | Category | On hand | Min | Order | Est. cost |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
        order_cost = 0
        for r in low:
            qty = max(r["min_stock"] - r["on_hand"], r["min_stock"])
            cost = qty * r["unit_cost_cents"]
            order_cost += cost
            lines.append(
                f"| {r['sku']} | {r['name']} | {r['category']} | {r['on_hand']} | "
                f"{r['min_stock']} | {qty} | {money(cost)} |"
            )
        lines += ["", f"**Estimated restock cost: {money(order_cost)}**", ""]
    else:
        lines += ["Every SKU is at or above its minimum. ", ""]

    # ---------------------------------------------------------- expiry watch
    cutoff = (date.today().toordinal() + expiry_days)
    cutoff_iso = date.fromordinal(cutoff).isoformat()
    expiring = conn.execute(
        "SELECT p.sku, p.name, l.lot_code, l.qty, l.expires_on, d.delivery_number "
        "  FROM delivery_lines l "
        "  JOIN products p   ON p.id = l.product_id "
        "  JOIN deliveries d ON d.id = l.delivery_id "
        " WHERE l.expires_on IS NOT NULL AND l.expires_on <= ? AND d.status <> 'void' "
        " ORDER BY l.expires_on",
        (cutoff_iso,),
    ).fetchall()
    lines += [f"## Expiring within {expiry_days} days", ""]
    if expiring:
        lines += [
            "| Expires | SKU | Product | Lot | Qty received | Delivery |",
            "| --- | --- | --- | --- | ---: | --- |",
        ]
        for r in expiring:
            lines.append(
                f"| {r['expires_on']} | {r['sku']} | {r['name']} | {r['lot_code']} | "
                f"{r['qty']} | {r['delivery_number']} |"
            )
    else:
        lines.append(f"No received lots expire by {cutoff_iso}.")
    lines.append("")

    # --------------------------------------------------------- recent receipts
    recent = conn.execute(
        "SELECT * FROM v_delivery_summary ORDER BY received_at DESC, id DESC LIMIT 15"
    ).fetchall()
    lines += [
        "## Recent deliveries",
        "",
        "| Delivery | Received | Supplier | Invoice | Lines | Units | Total | Excise |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for r in recent:
        lines.append(
            f"| {r['delivery_number']} | {r['received_at'][:10]} | {r['supplier']} | "
            f"{r['invoice_number']} | {r['line_count']} | {r['units']:,} | "
            f"{money(r['total_cents'])} | {money(r['excise_cents'])} |"
        )
    lines.append("")

    drift = inventory_drift(conn)
    lines += ["## Ledger integrity", ""]
    if drift:
        lines += ["| SKU | on_hand | movement ledger |", "| --- | ---: | ---: |"]
        for r in drift:
            lines.append(f"| {r['sku']} | {r['on_hand']} | {r['ledger']} |")
        lines += ["", "**on_hand disagrees with the movement ledger — investigate.**", ""]
    else:
        lines.append("`inventory.on_hand` matches `SUM(stock_movements.qty_delta)` for every SKU.")
        lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="report.py",
                                    description="Write the smoke shop inventory report.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out", type=Path, default=None,
                    help="output path (default reports/inventory-YYYY-MM-DD.md)")
    ap.add_argument("--stdout", action="store_true", help="print markdown to stdout")
    ap.add_argument("--expiry-days", type=int, default=90,
                    help="window for the expiry watch (default 90)")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"{args.db} does not exist — run run_deliveries.py or open_db() first")
        return 1

    conn = open_db(args.db)
    report = build_report(conn, args.expiry_days)
    t = totals(conn)
    conn.close()

    if args.stdout:
        print(report)
        return 0

    stamp = datetime.now(timezone.utc).date().isoformat()
    out = args.out or args.db.parent / "reports" / f"inventory-{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    print(
        f"wrote {out} — {t['deliveries']} deliveries, {t['units_on_hand']:,} units on hand, "
        f"{t['below_min']} SKU(s) below minimum"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
