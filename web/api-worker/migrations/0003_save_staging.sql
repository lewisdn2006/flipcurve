-- A scan's save arrives in pieces (one small request each, to stay inside the free plan's CPU per
-- request), staged here and moved into place by one last request, so a save is still all or nothing.
-- Pieces of a save that never committed are cleared after a day.
CREATE TABLE stage_rows (
  save_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  platform TEXT NOT NULL,
  n INTEGER NOT NULL,
  row_json TEXT NOT NULL,
  created_at INTEGER NOT NULL   -- unix seconds
);
CREATE INDEX idx_stage_rows ON stage_rows (save_id, slug, platform);
CREATE TABLE stage_seen (
  save_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  item_id TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_checked TEXT,
  resolved TEXT,
  relevant INTEGER NOT NULL DEFAULT 0,
  rec_json TEXT NOT NULL,
  n INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX idx_stage_seen ON stage_seen (save_id, slug);
