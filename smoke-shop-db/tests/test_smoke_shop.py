#!/usr/bin/env python3
"""Tests for the smoke shop DB. Stdlib only:

    cd smoke-shop-db && python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import run_deliveries  # noqa: E402
from db import (  # noqa: E402
    init_db,
    inventory_drift,
    low_stock,
    money,
    open_db,
    receive_delivery,
    receive_pending,
    totals,
)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.conn = init_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def line(self, product_id=1, qty=6, unit_cost=480, lot="LOT-TEST-A1", expires=None):
        return {
            "product_id": product_id,
            "qty": qty,
            "unit_cost_cents": unit_cost,
            "lot_code": lot,
            "expires_on": expires,
        }

    def on_hand(self, product_id=1):
        row = self.conn.execute(
            "SELECT on_hand FROM inventory WHERE product_id = ?", (product_id,)
        ).fetchone()
        return row["on_hand"]

    def receive(self, lines=None, invoice="INV-T-0001", at="2026-11-10T12:00:00Z"):
        return receive_delivery(
            self.conn,
            supplier_id=1,
            invoice_number=invoice,
            received_at=at,
            lines=lines or [self.line()],
        )


class TestSeedAndSchema(Base):
    def test_seed_populates_catalog(self):
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM suppliers").fetchone()["c"], 5)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM products").fetchone()["c"], 23)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) c FROM inventory").fetchone()["c"], 23)

    def test_init_is_idempotent(self):
        """Re-applying schema + seed must not duplicate rows."""
        before = totals(self.conn)
        init_db(self.db_path)
        conn2 = open_db(self.db_path)
        self.addCleanup(conn2.close)
        self.assertEqual(totals(conn2), before)
        self.assertEqual(
            conn2.execute("SELECT COUNT(*) c FROM supplier_products").fetchone()["c"],
            self.conn.execute("SELECT COUNT(*) c FROM supplier_products").fetchone()["c"],
        )

    def test_opening_stock_matches_ledger(self):
        self.assertEqual(inventory_drift(self.conn), [])
        self.assertEqual(self.on_hand(1), 40)

    def test_seed_has_deliberate_low_stock(self):
        self.assertGreater(len(low_stock(self.conn)), 0)

    def test_reset_rebuilds_empty_ledger(self):
        self.receive()
        self.conn.close()
        self.conn = init_db(self.db_path, reset=True)
        self.assertEqual(totals(self.conn)["deliveries"], 0)
        self.assertEqual(self.on_hand(1), 40)


class TestReceiving(Base):
    def test_receive_adds_stock_and_movement(self):
        before = self.on_hand(1)
        did = self.receive([self.line(qty=12)])
        self.assertEqual(self.on_hand(1), before + 12)
        movement = self.conn.execute(
            "SELECT * FROM stock_movements WHERE product_id = 1 AND reason = 'delivery'"
        ).fetchone()
        self.assertEqual(movement["qty_delta"], 12)
        self.assertEqual(
            self.conn.execute(
                "SELECT delivery_id FROM delivery_lines WHERE id = ?",
                (movement["delivery_line_id"],),
            ).fetchone()["delivery_id"],
            did,
        )
        self.assertEqual(inventory_drift(self.conn), [])

    def test_total_maintained_by_trigger(self):
        did = self.receive([self.line(qty=5, unit_cost=480), self.line(product_id=2, qty=3, unit_cost=2150)])
        expected = 5 * 480 + 3 * 2150
        self.assertEqual(self.conn.execute(
            "SELECT total_cents FROM deliveries WHERE id = ?", (did,)
        ).fetchone()["total_cents"], expected)
        # ...and it stays correct after a line is deleted.
        self.conn.execute("DELETE FROM delivery_lines WHERE delivery_id = ? AND product_id = 2", (did,))
        self.assertEqual(self.conn.execute(
            "SELECT total_cents FROM deliveries WHERE id = ?", (did,)
        ).fetchone()["total_cents"], 5 * 480)

    def test_duplicate_invoice_is_rejected_without_side_effects(self):
        self.receive(invoice="INV-DUP-1")
        before = self.on_hand(1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.receive(invoice="INV-DUP-1", lines=[self.line(qty=100, lot="LOT-OTHER-B2")])
        self.assertEqual(self.on_hand(1), before)
        self.assertEqual(totals(self.conn)["deliveries"], 1)
        self.assertEqual(totals(self.conn)["lines"], 1)

    def test_invalid_qty_rolls_back_whole_delivery(self):
        with self.assertRaises(ValueError):
            self.receive([self.line(qty=4), self.line(product_id=2, qty=0, lot="LOT-Z-9")])
        self.assertEqual(totals(self.conn)["deliveries"], 0)
        self.assertEqual(self.on_hand(1), 40)

    def test_delivery_numbers_are_sequential_per_year(self):
        first = self.receive(invoice="A-1", at="2026-11-01T10:00:00Z")
        second = self.receive(invoice="A-2", at="2026-11-02T10:00:00Z")
        next_year = self.receive(invoice="A-3", at="2027-01-04T10:00:00Z")
        numbers = [r["delivery_number"] for r in self.conn.execute(
            "SELECT delivery_number FROM deliveries ORDER BY id")]
        self.assertEqual(numbers, ["DLV-2026-0001", "DLV-2026-0002", "DLV-2027-0001"])
        self.assertEqual(sorted((first, second, next_year)), [first, second, next_year])

    def test_pending_delivery_counts_only_once_promoted(self):
        did = receive_delivery(
            self.conn,
            supplier_id=2,
            invoice_number="INV-PEND-1",
            received_at="2026-11-05T09:00:00Z",
            lines=[self.line(qty=10, lot="LOT-PEND-1")],
            status="pending",
        )
        self.assertEqual(self.on_hand(1), 40)  # not counted yet
        receive_pending(self.conn, did, at="2026-11-06T09:00:00Z")
        self.assertEqual(self.on_hand(1), 50)
        with self.assertRaises(ValueError):
            receive_pending(self.conn, did, at="2026-11-06T09:00:00Z")  # no double count
        self.assertEqual(self.on_hand(1), 50)

    def test_restock_clears_low_stock_flag(self):
        self.assertIn("VPE-1002", [r["sku"] for r in low_stock(self.conn)])
        self.receive([self.line(product_id=2, qty=12, unit_cost=2150, lot="LOT-BOX-1")])
        self.assertNotIn("VPE-1002", [r["sku"] for r in low_stock(self.conn)])


class TestGenerator(Base):
    def run_generator(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = run_deliveries.main(["--db", str(self.db_path), "--json", *argv])
        self.conn.close()
        self.conn = open_db(self.db_path)
        self.assertEqual(code, 0)
        return json.loads(buf.getvalue())

    def test_generates_requested_deliveries(self):
        out = self.run_generator("--count", "4", "--seed", "11")
        self.assertEqual(out["count"], 4)
        self.assertEqual(totals(self.conn)["deliveries"], 4)
        self.assertEqual(inventory_drift(self.conn), [])
        for d in out["deliveries"]:
            self.assertTrue(d["delivery_number"].startswith("DLV-"))
            self.assertGreaterEqual(len(d["lines"]), 3)
            self.assertLessEqual(len(d["lines"]), 7)
            self.assertEqual(
                d["total_cents"], sum(line["line_total_cents"] for line in d["lines"])
            )
            self.assertGreater(d["units"], 0)
            # No duplicate SKUs inside one delivery.
            skus = [line["sku"] for line in d["lines"]]
            self.assertEqual(len(skus), len(set(skus)))

    def test_perishables_get_lots_and_expiry(self):
        out = self.run_generator("--count", "6", "--seed", "3")
        perishable_seen = False
        for d in out["deliveries"]:
            for line in d["lines"]:
                self.assertTrue(line["lot_code"].startswith("LOT-"))
                if line["category"] in ("disposable", "e_liquid", "nicotine_pouch", "cbd_topical"):
                    perishable_seen = True
                    self.assertIsNotNone(line["expires_on"], line)
                    self.assertGreater(line["expires_on"], d["received_at"][:10])
                else:
                    self.assertIsNone(line["expires_on"], line)
        self.assertTrue(perishable_seen)

    def test_seed_makes_runs_reproducible(self):
        first = self.run_generator("--count", "3", "--seed", "99")
        self.conn.close()
        self.conn = init_db(self.db_path, reset=True)
        second = self.run_generator("--count", "3", "--seed", "99")
        strip = lambda run: [  # noqa: E731 — received_at depends on wall clock
            (d["supplier"], d["invoice_number"],
             [(l["sku"], l["qty"], l["unit_cost_cents"]) for l in d["lines"]])
            for d in run["deliveries"]
        ]
        self.assertEqual(strip(first), strip(second))

    def test_bad_count_rejected(self):
        with self.assertRaises(SystemExit):
            run_deliveries.main(["--db", str(self.db_path), "--count", "0"])


class TestMoney(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(money(0), "$0.00")
        self.assertEqual(money(5), "$0.05")
        self.assertEqual(money(1299), "$12.99")
        self.assertEqual(money(123456789), "$1,234,567.89")
        self.assertEqual(money(-2500), "-$25.00")


if __name__ == "__main__":
    unittest.main()
