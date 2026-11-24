"""Data layer for the smoke shop DB.

Python 3.11 + stdlib sqlite3 only. Money is integer cents end to end.

Typical use:
    from db import open_db, receive_delivery
    conn = open_db("smoke_shop.db")     # creates + seeds on first run
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable, Mapping, Sequence

HERE = Path(__file__).resolve().parent
SCHEMA = HERE / "schema.sql"
SEED = HERE / "seed.sql"
DEFAULT_DB = HERE / "smoke_shop.db"

STATUSES = ("pending", "received", "reconciled", "void")
# Categories where stock is date-sensitive and gets lot + expiry tracking.
PERISHABLE = ("disposable", "e_liquid", "nicotine_pouch", "cbd_topical")


# --------------------------------------------------------------------- money
def money(cents: int) -> str:
    """123456 -> '$1,234.56'. Negative amounts render as '-$1,234.56'."""
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(int(cents)), 100)
    return f"{sign}${whole:,}.{frac:02d}"


# ------------------------------------------------------------------ plumbing
def connect(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _is_initialised(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'products'"
    ).fetchone()
    return row is not None


def init_db(path: str | Path = DEFAULT_DB, *, reset: bool = False) -> sqlite3.Connection:
    """Create the schema and apply the seed. Safe to call repeatedly."""
    path = Path(path)
    if reset and path.exists():
        path.unlink()
    conn = connect(path)
    conn.executescript(SCHEMA.read_text())
    conn.executescript(SEED.read_text())
    return conn


def open_db(path: str | Path = DEFAULT_DB) -> sqlite3.Connection:
    """Open the DB, initialising it on first use."""
    conn = connect(path)
    if not _is_initialised(conn):
        conn.close()
        return init_db(path)
    return conn


# ------------------------------------------------------- delivery numbering
def next_delivery_number(conn: sqlite3.Connection, received_at: str) -> str:
    """DLV-<year>-<zero-padded sequence within that year>."""
    year = received_at[:4]
    row = conn.execute(
        "SELECT MAX(CAST(substr(delivery_number, 10) AS INTEGER)) AS seq "
        "  FROM deliveries WHERE delivery_number LIKE ?",
        (f"DLV-{year}-%",),
    ).fetchone()
    seq = (row["seq"] or 0) + 1
    return f"DLV-{year}-{seq:04d}"


# ------------------------------------------------------------- receiving
def receive_delivery(
    conn: sqlite3.Connection,
    *,
    supplier_id: int,
    invoice_number: str,
    received_at: str,
    lines: Sequence[Mapping],
    status: str = "received",
    delivery_number: str | None = None,
) -> int:
    """Insert one delivery and its lines atomically; return the delivery id.

    lines: [{product_id, qty, unit_cost_cents, lot_code, expires_on?}, ...]

    With status='received' (the default) stock is added immediately: an
    append-only stock_movements row per line plus the inventory balance.
    A 'pending' delivery is recorded but not counted until it is received
    through receive_pending().
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    if not lines:
        raise ValueError("a delivery needs at least one line")

    with conn:  # explicit transaction, rolled back as a whole on any error
        conn.execute("BEGIN IMMEDIATE")
        if delivery_number is None:
            delivery_number = next_delivery_number(conn, received_at)
        cur = conn.execute(
            "INSERT INTO deliveries "
            "(delivery_number, supplier_id, invoice_number, received_at, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (delivery_number, supplier_id, invoice_number, received_at, status, received_at),
        )
        delivery_id = int(cur.lastrowid)

        for line in lines:
            qty = int(line["qty"])
            unit_cost = int(line["unit_cost_cents"])
            if qty <= 0:
                raise ValueError(f"qty must be positive, got {qty}")
            if unit_cost < 0:
                raise ValueError(f"unit_cost_cents must be >= 0, got {unit_cost}")

            line_cur = conn.execute(
                "INSERT INTO delivery_lines "
                "(delivery_id, product_id, qty, unit_cost_cents, lot_code, expires_on) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    delivery_id,
                    int(line["product_id"]),
                    qty,
                    unit_cost,
                    line["lot_code"],
                    line.get("expires_on"),
                ),
            )
            if status == "received":
                _add_stock(
                    conn,
                    product_id=int(line["product_id"]),
                    qty=qty,
                    delivery_line_id=int(line_cur.lastrowid),
                    at=received_at,
                )
    return delivery_id


def _add_stock(
    conn: sqlite3.Connection,
    *,
    product_id: int,
    qty: int,
    delivery_line_id: int | None,
    at: str,
) -> None:
    conn.execute(
        "INSERT INTO stock_movements "
        "(product_id, delivery_line_id, qty_delta, reason, created_at) "
        "VALUES (?, ?, ?, 'delivery', ?)",
        (product_id, delivery_line_id, qty, at),
    )
    conn.execute(
        "INSERT INTO inventory (product_id, on_hand, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(product_id) DO UPDATE SET "
        "  on_hand = inventory.on_hand + excluded.on_hand, "
        "  updated_at = excluded.updated_at",
        (product_id, qty, at),
    )


def receive_pending(conn: sqlite3.Connection, delivery_id: int, *, at: str) -> None:
    """Promote a 'pending' delivery to 'received' and add its stock once."""
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, received_at FROM deliveries WHERE id = ?", (delivery_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"delivery {delivery_id} not found")
        if row["status"] != "pending":
            raise ValueError(f"delivery {delivery_id} is {row['status']}, not pending")

        for line in conn.execute(
            "SELECT id, product_id, qty FROM delivery_lines WHERE delivery_id = ?",
            (delivery_id,),
        ).fetchall():
            _add_stock(
                conn,
                product_id=line["product_id"],
                qty=line["qty"],
                delivery_line_id=line["id"],
                at=at,
            )
        conn.execute(
            "UPDATE deliveries SET status = 'received', received_at = ? WHERE id = ?",
            (at, delivery_id),
        )


# ------------------------------------------------------------- inspection
def low_stock(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM v_inventory_status WHERE below_min ORDER BY category, sku"
    ).fetchall()


def inventory_drift(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Products whose on_hand disagrees with the movement ledger (should be empty)."""
    return conn.execute(
        "SELECT p.sku, i.on_hand, "
        "       COALESCE((SELECT SUM(m.qty_delta) FROM stock_movements m "
        "                  WHERE m.product_id = p.id), 0) AS ledger "
        "  FROM products p JOIN inventory i ON i.product_id = p.id "
        " WHERE i.on_hand <> COALESCE((SELECT SUM(m.qty_delta) FROM stock_movements m "
        "                               WHERE m.product_id = p.id), 0)"
    ).fetchall()


def totals(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT (SELECT COUNT(*) FROM deliveries)                    AS deliveries, "
        "       (SELECT COUNT(*) FROM delivery_lines)                 AS lines, "
        "       (SELECT COALESCE(SUM(total_cents), 0) FROM deliveries) AS spend_cents, "
        "       (SELECT COALESCE(SUM(on_hand), 0) FROM inventory)     AS units_on_hand, "
        "       (SELECT COUNT(*) FROM v_inventory_status WHERE below_min) AS below_min"
    ).fetchone()
    return dict(row)
