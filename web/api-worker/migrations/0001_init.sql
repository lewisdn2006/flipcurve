-- What the laptop kept in files (NO_LAPTOP_PLAN.md, step 2). Where a file held JSON records,
-- the row keeps the whole record as JSON next to the columns the queries need, so the ported
-- code can read it the same way the Python did.

-- backend/db/ebay_calibration.sqlite: start time by item number, for dating sold listings
CREATE TABLE ebay_calibration_points (
  item_id INTEGER PRIMARY KEY,
  block TEXT NOT NULL,          -- first three digits (ebay_calibration.block_of)
  start_ts REAL NOT NULL,       -- unix seconds
  source TEXT
);
CREATE INDEX idx_ebay_calibration_block ON ebay_calibration_points (block, item_id);

-- backend/db/vinted_calibration.json: {item_id: [earliest, latest] upload time, unix seconds}
CREATE TABLE vinted_calibration (
  item_id INTEGER PRIMARY KEY,
  lo_ts REAL NOT NULL,
  hi_ts REAL NOT NULL
);

-- backend/scrapers/cache_seed/<slug>_<platform>.json: the last scan of each search
CREATE TABLE scan_cache (
  slug TEXT NOT NULL,           -- orchestrator.slug(query)
  platform TEXT NOT NULL,       -- ebay | vinted
  query TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  extra_json TEXT NOT NULL DEFAULT '{}',  -- "stats", "source": everything saved beside the rows
  PRIMARY KEY (slug, platform)
);
CREATE TABLE scan_cache_rows (
  slug TEXT NOT NULL,
  platform TEXT NOT NULL,
  n INTEGER NOT NULL,           -- position in the saved list
  row_json TEXT NOT NULL,       -- one ListingRow
  PRIMARY KEY (slug, platform, n)
);

-- the "seen" map in Vinted cache files: listings to re-open later to find out if they sold
CREATE TABLE vinted_seen (
  slug TEXT NOT NULL,
  item_id TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_checked TEXT,
  resolved TEXT,                -- e.g. "sold"; NULL while still being followed
  relevant INTEGER NOT NULL DEFAULT 0,
  rec_json TEXT NOT NULL,       -- the whole record (card, last_active, sold_seen_at, ...)
  PRIMARY KEY (slug, item_id)
);
CREATE INDEX idx_vinted_seen_due ON vinted_seen (resolved, last_checked);

-- backend/db/model_cooldowns.json: AI models out of allowance until a time
CREATE TABLE model_cooldowns (
  model TEXT PRIMARY KEY,
  until REAL NOT NULL,          -- unix seconds
  why TEXT,
  since REAL
);
