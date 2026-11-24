-- Seed data for the smoke shop DB: suppliers, catalog, supplier costs, opening stock.
-- Idempotent: every insert is OR IGNORE with an explicit id, so re-applying is safe.
-- Categories: vape_device, disposable, e_liquid, nicotine_pouch, glass,
--             accessory, rolling_paper, cbd_topical
-- Prices are integer cents. excise_bps are basis points of wholesale (2500 = 25%).

BEGIN;

-- ---------------------------------------------------------------- suppliers
INSERT OR IGNORE INTO suppliers (id, name, contact_email, phone, lead_time_days) VALUES
    (1, 'Cascade Distribution',        'orders@cascade-dist.example',    '+1-503-555-0142', 4),
    (2, 'Desert Vapor Supply',         'orders@desertvapor.example',     '+1-702-555-0188', 2),
    (3, 'Pacific Coast Wholesale',     'sales@pcwholesale.example',      '+1-206-555-0119', 5),
    (4, 'Rocky Mountain Imports',      'hello@rmimports.example',        '+1-303-555-0164', 6),
    (5, 'Sunbelt Tobacco & Accessories','orders@sunbelt.example',        '+1-602-555-0133', 3);

-- ----------------------------------------------------------------- products
INSERT OR IGNORE INTO products
    (id, sku, name, brand, category, unit_cost_cents, retail_cents, min_stock, excise_bps, age_restricted) VALUES
    ( 1, 'VPE-1001', 'Aero Pen Battery 650mAh',       'Aero',      'vape_device',      480, 1299, 24,    0, 1),
    ( 2, 'VPE-1002', 'Cloudbox 40W Box Mod',          'Cloudbox',  'vape_device',     2150, 4999, 12,    0, 1),
    ( 3, 'DSP-2001', 'Puff Peak 5K Disposable',       'Puff Peak', 'disposable',       690, 1899, 48, 2500, 1),
    ( 4, 'DSP-2002', 'Puff Peak 10K Pro',             'Puff Peak', 'disposable',      1150, 2799, 36, 2500, 1),
    ( 5, 'DSP-2003', 'Nova Bar 6K',                   'Nova',      'disposable',       780, 1999, 48, 2500, 1),
    ( 6, 'ELQ-3001', 'Frost Mango 30ml 35mg',         'Frost',     'e_liquid',         620, 1699, 40, 3000, 1),
    ( 7, 'ELQ-3002', 'Blue Razz 30ml 50mg',           'Frost',     'e_liquid',         640, 1799, 40, 3000, 1),
    ( 8, 'ELQ-3003', 'Menthol Chill 30ml 24mg',       'Northwind', 'e_liquid',         590, 1599, 30, 3000, 1),
    ( 9, 'ELQ-3004', 'Tobacco Gold 60ml 12mg',        'Northwind', 'e_liquid',         980, 2499, 24, 3000, 1),
    (10, 'NPU-4001', 'Ice Mint 6mg 5pk',              'Polar',     'nicotine_pouch',   380,  999, 60, 3500, 1),
    (11, 'NPU-4002', 'Wintergreen 3mg 5pk',           'Polar',     'nicotine_pouch',   370,  999, 60, 3500, 1),
    (12, 'NPU-4003', 'Citrus Burst 9mg 5pk',          'Polar',     'nicotine_pouch',   390, 1049, 48, 3500, 1),
    (13, 'GLS-5001', 'Beaker Bong 12in',              'Highwater', 'glass',           1450, 3999,  6,    0, 1),
    (14, 'GLS-5002', 'Fumed Hand Pipe',               'Highwater', 'glass',            520, 1799, 12,    0, 1),
    (15, 'GLS-5003', 'Mini Dab Rig',                  'Highwater', 'glass',           1980, 5499,  4,    0, 1),
    (16, 'ACC-6001', 'Grinder 4pc 2.5in',             'Stash Lab', 'accessory',        640, 1999, 18,    0, 1),
    (17, 'ACC-6002', 'Torch Lighter',                 'Stash Lab', 'accessory',        410, 1299, 24,    0, 1),
    (18, 'ACC-6003', 'Rolling Tray Metal',            'Stash Lab', 'accessory',        320,  999, 20,    0, 0),
    (19, 'ACC-6004', 'Replacement Coils 0.6ohm 5pk',  'Aero',      'accessory',        550, 1499, 30,    0, 1),
    (20, 'PAP-7001', 'King Size Papers 32ct',         'Riverbend', 'rolling_paper',     95,  349, 80,    0, 0),
    (21, 'PAP-7002', 'Pre-Rolled Cones 1.25 6pk',     'Riverbend', 'rolling_paper',    210,  699, 50,    0, 0),
    (22, 'CBD-8001', 'CBD Balm 500mg',                'Meadow',    'cbd_topical',      720, 2499, 16,    0, 0),
    (23, 'CBD-8002', 'CBD Tincture 1000mg',           'Meadow',    'cbd_topical',     1180, 3999, 12,    0, 0);

-- ------------------------------------------------- supplier catalog + costs
INSERT OR IGNORE INTO supplier_products (supplier_id, product_id, unit_cost_cents) VALUES
    -- Cascade Distribution: devices, disposables, e-liquid
    (1,  1,  480), (1,  2, 2150), (1,  3,  690), (1,  4, 1150), (1,  5,  780),
    (1,  6,  620), (1,  7,  640), (1,  8,  590), (1,  9,  980),
    -- Desert Vapor Supply: devices, e-liquid, pouches, coils
    (2,  1,  465), (2,  6,  610), (2,  7,  630), (2,  8,  575), (2,  9,  955),
    (2, 10,  380), (2, 11,  370), (2, 12,  390), (2, 19,  540),
    -- Pacific Coast Wholesale: glass, accessories, papers, CBD
    (3, 13, 1450), (3, 14,  520), (3, 15, 1980), (3, 16,  640), (3, 17,  410),
    (3, 18,  320), (3, 20,   95), (3, 21,  210), (3, 22,  720), (3, 23, 1180),
    -- Rocky Mountain Imports: glass, accessories, CBD
    (4, 13, 1495), (4, 14,  535), (4, 15, 2040), (4, 16,  655), (4, 18,  330),
    (4, 22,  700), (4, 23, 1150),
    -- Sunbelt Tobacco & Accessories: papers, pouches, disposables, accessories
    (5,  3,  710), (5,  5,  795), (5, 10,  395), (5, 11,  385), (5, 12,  405),
    (5, 17,  425), (5, 18,  335), (5, 20,   98), (5, 21,  215);

-- ------------------------------------------------------------ opening stock
-- A few SKUs are deliberately below min_stock so the reorder report has work.
INSERT OR IGNORE INTO inventory (product_id, on_hand, updated_at) VALUES
    ( 1,  40, '2026-11-01T12:00:00Z'), ( 2,   9, '2026-11-01T12:00:00Z'),
    ( 3, 120, '2026-11-01T12:00:00Z'), ( 4,  60, '2026-11-01T12:00:00Z'),
    ( 5,  22, '2026-11-01T12:00:00Z'), ( 6,  75, '2026-11-01T12:00:00Z'),
    ( 7,  52, '2026-11-01T12:00:00Z'), ( 8,  18, '2026-11-01T12:00:00Z'),
    ( 9,  40, '2026-11-01T12:00:00Z'), (10, 130, '2026-11-01T12:00:00Z'),
    (11,  95, '2026-11-01T12:00:00Z'), (12,  30, '2026-11-01T12:00:00Z'),
    (13,   8, '2026-11-01T12:00:00Z'), (14,   5, '2026-11-01T12:00:00Z'),
    (15,   2, '2026-11-01T12:00:00Z'), (16,  24, '2026-11-01T12:00:00Z'),
    (17,  30, '2026-11-01T12:00:00Z'), (18,  12, '2026-11-01T12:00:00Z'),
    (19,  44, '2026-11-01T12:00:00Z'), (20, 200, '2026-11-01T12:00:00Z'),
    (21,  88, '2026-11-01T12:00:00Z'), (22,  10, '2026-11-01T12:00:00Z'),
    (23,  14, '2026-11-01T12:00:00Z');

-- Opening balances are themselves stock movements, so on_hand is always
-- reproducible from the audit trail (sum(qty_delta) == on_hand).
INSERT OR IGNORE INTO stock_movements (id, product_id, delivery_line_id, qty_delta, reason, created_at)
SELECT p.id, p.id, NULL, i.on_hand, 'adjustment', i.updated_at
  FROM products p
  JOIN inventory i ON i.product_id = p.id;

COMMIT;
