-- Smoke shop database schema (SQLite 3.31+).
-- Money is stored as integer cents everywhere. Never floats.
-- Apply with:  python3 -c "from db import init_db; init_db('smoke_shop.db', reset=True)"

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- suppliers
CREATE TABLE IF NOT EXISTS suppliers (
    id             INTEGER PRIMARY KEY,
    name           TEXT    NOT NULL UNIQUE,
    contact_email  TEXT    NOT NULL,
    phone          TEXT,
    lead_time_days INTEGER NOT NULL DEFAULT 3 CHECK (lead_time_days >= 0)
);

-- ----------------------------------------------------------------- products
-- Categories are deliberately a closed set: the receiving workflow and the
-- reorder report both branch on them (perishables get lot/expiry tracking,
-- nicotine goods carry excise).
CREATE TABLE IF NOT EXISTS products (
    id              INTEGER PRIMARY KEY,
    sku             TEXT    NOT NULL UNIQUE,
    name            TEXT    NOT NULL,
    brand           TEXT    NOT NULL,
    category        TEXT    NOT NULL CHECK (category IN (
                        'vape_device', 'disposable', 'e_liquid', 'nicotine_pouch',
                        'glass', 'accessory', 'rolling_paper', 'cbd_topical')),
    unit_cost_cents INTEGER NOT NULL CHECK (unit_cost_cents >= 0),
    retail_cents    INTEGER NOT NULL CHECK (retail_cents > 0),
    min_stock       INTEGER NOT NULL DEFAULT 0 CHECK (min_stock >= 0),
    -- Excise as basis points of wholesale cost (2500 bps = 25%). 0 for
    -- accessories and hardware, > 0 for consumable nicotine.
    excise_bps      INTEGER NOT NULL DEFAULT 0 CHECK (excise_bps BETWEEN 0 AND 10000),
    age_restricted  INTEGER NOT NULL DEFAULT 1 CHECK (age_restricted IN (0, 1))
);

-- Which supplier carries which product, at what wholesale cost.
CREATE TABLE IF NOT EXISTS supplier_products (
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    product_id      INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    unit_cost_cents INTEGER NOT NULL CHECK (unit_cost_cents >= 0),
    PRIMARY KEY (supplier_id, product_id)
);

-- ---------------------------------------------------------------- inventory
CREATE TABLE IF NOT EXISTS inventory (
    product_id INTEGER PRIMARY KEY REFERENCES products(id) ON DELETE CASCADE,
    on_hand    INTEGER NOT NULL DEFAULT 0 CHECK (on_hand >= 0),
    updated_at TEXT    NOT NULL
);

-- --------------------------------------------------------------- deliveries
CREATE TABLE IF NOT EXISTS deliveries (
    id              INTEGER PRIMARY KEY,
    delivery_number TEXT    NOT NULL UNIQUE,
    supplier_id     INTEGER NOT NULL REFERENCES suppliers(id),
    invoice_number  TEXT    NOT NULL,
    received_at     TEXT    NOT NULL,          -- ISO-8601 UTC
    status          TEXT    NOT NULL DEFAULT 'received'
                            CHECK (status IN ('pending', 'received', 'reconciled', 'void')),
    -- Denormalised running total, maintained by trg_delivery_totals_*.
    total_cents     INTEGER NOT NULL DEFAULT 0 CHECK (total_cents >= 0),
    created_at      TEXT    NOT NULL,
    -- One invoice is received exactly once per supplier: this is the guard
    -- against double-counting stock.
    UNIQUE (supplier_id, invoice_number)
);

CREATE TABLE IF NOT EXISTS delivery_lines (
    id              INTEGER PRIMARY KEY,
    delivery_id     INTEGER NOT NULL REFERENCES deliveries(id) ON DELETE CASCADE,
    product_id      INTEGER NOT NULL REFERENCES products(id),
    qty             INTEGER NOT NULL CHECK (qty > 0),
    unit_cost_cents INTEGER NOT NULL CHECK (unit_cost_cents >= 0),
    lot_code        TEXT    NOT NULL,
    expires_on      TEXT,                      -- NULL for non-perishables
    line_total_cents INTEGER GENERATED ALWAYS AS (qty * unit_cost_cents) STORED,
    UNIQUE (delivery_id, product_id, lot_code)
);

-- Append-only audit trail. Every change to on_hand has a row here, so the
-- inventory can always be rebuilt from history.
CREATE TABLE IF NOT EXISTS stock_movements (
    id               INTEGER PRIMARY KEY,
    product_id       INTEGER NOT NULL REFERENCES products(id),
    delivery_line_id INTEGER REFERENCES delivery_lines(id) ON DELETE SET NULL,
    qty_delta        INTEGER NOT NULL,
    reason           TEXT    NOT NULL CHECK (reason IN (
                         'delivery', 'sale', 'adjustment', 'return', 'damage')),
    created_at       TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deliveries_received_at ON deliveries(received_at);
CREATE INDEX IF NOT EXISTS idx_deliveries_supplier    ON deliveries(supplier_id);
CREATE INDEX IF NOT EXISTS idx_lines_delivery         ON delivery_lines(delivery_id);
CREATE INDEX IF NOT EXISTS idx_lines_product          ON delivery_lines(product_id);
CREATE INDEX IF NOT EXISTS idx_movements_product      ON stock_movements(product_id);
CREATE INDEX IF NOT EXISTS idx_movements_created_at   ON stock_movements(created_at);

-- --------------------------------------------------- totals maintenance
CREATE TRIGGER IF NOT EXISTS trg_delivery_totals_ai
AFTER INSERT ON delivery_lines
BEGIN
    UPDATE deliveries
       SET total_cents = (SELECT COALESCE(SUM(line_total_cents), 0)
                            FROM delivery_lines WHERE delivery_id = NEW.delivery_id)
     WHERE id = NEW.delivery_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_delivery_totals_au
AFTER UPDATE ON delivery_lines
BEGIN
    UPDATE deliveries
       SET total_cents = (SELECT COALESCE(SUM(line_total_cents), 0)
                            FROM delivery_lines WHERE delivery_id = NEW.delivery_id)
     WHERE id = NEW.delivery_id;
    UPDATE deliveries
       SET total_cents = (SELECT COALESCE(SUM(line_total_cents), 0)
                            FROM delivery_lines WHERE delivery_id = OLD.delivery_id)
     WHERE id = OLD.delivery_id AND OLD.delivery_id <> NEW.delivery_id;
END;

CREATE TRIGGER IF NOT EXISTS trg_delivery_totals_ad
AFTER DELETE ON delivery_lines
BEGIN
    UPDATE deliveries
       SET total_cents = (SELECT COALESCE(SUM(line_total_cents), 0)
                            FROM delivery_lines WHERE delivery_id = OLD.delivery_id)
     WHERE id = OLD.delivery_id;
END;

-- ---------------------------------------------------------------- reporting
CREATE VIEW IF NOT EXISTS v_inventory_status AS
SELECT p.id                AS product_id,
       p.sku,
       p.name,
       p.brand,
       p.category,
       COALESCE(i.on_hand, 0)                 AS on_hand,
       p.min_stock,
       COALESCE(i.on_hand, 0) < p.min_stock   AS below_min,
       p.unit_cost_cents,
       p.retail_cents,
       COALESCE(i.on_hand, 0) * p.unit_cost_cents  AS cost_value_cents,
       COALESCE(i.on_hand, 0) * p.retail_cents     AS retail_value_cents,
       i.updated_at
  FROM products p
  LEFT JOIN inventory i ON i.product_id = p.id;

CREATE VIEW IF NOT EXISTS v_delivery_summary AS
SELECT d.id,
       d.delivery_number,
       d.received_at,
       d.status,
       s.name                AS supplier,
       d.invoice_number,
       COUNT(l.id)           AS line_count,
       COALESCE(SUM(l.qty), 0)             AS units,
       d.total_cents,
       COALESCE(SUM(l.unit_cost_cents * l.qty * p.excise_bps / 10000), 0)
                            AS excise_cents
  FROM deliveries d
  JOIN suppliers s        ON s.id = d.supplier_id
  LEFT JOIN delivery_lines l ON l.delivery_id = d.id
  LEFT JOIN products p       ON p.id = l.product_id
 GROUP BY d.id;
