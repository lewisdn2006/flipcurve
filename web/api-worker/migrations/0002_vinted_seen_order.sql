-- Keep the order listings were first seen in (the Python's dict order), which decides the order of
-- the rows built from the history.
ALTER TABLE vinted_seen ADD COLUMN n INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_vinted_seen_order ON vinted_seen (slug, n);
