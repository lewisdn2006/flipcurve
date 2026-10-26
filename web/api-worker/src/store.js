// Where the scan data lives: D1 in the Worker (migrations/), or memory in tests. Both have the same
// methods. A saved search ("cache", as the Python called its files) is
//   {query, fetched_at, rows, stats?, source?, seen?: Map(item id -> history record)}

const parseJson = (s, fallback) => { try { return JSON.parse(s); } catch { return fallback; } };

export class MemoryStore {
  constructor() {
    this.points = new Map();   // item id -> [block, ts, source]
    this.vcal = new Map();     // item id -> [lo, hi]
    this.caches = new Map();   // "slug|platform" -> saved search (deep copies)
  }

  async ebayBlock(block) {
    const rows = [...this.points].filter(([, p]) => p[0] === block).sort((a, b) => a[0] - b[0]);
    return { ids: rows.map(([id]) => id), ts: rows.map(([, p]) => p[1]) };
  }

  async ebayAdd(points, source) {
    let n = 0;
    for (const [id, block, ts] of points) if (!this.points.has(id)) { this.points.set(id, [block, ts, source]); n++; }
    return n;
  }

  async vintedCalibration() { return new Map(this.vcal); }

  async saveVintedCalibration(cal) {
    for (const id of cal.changed) this.vcal.set(id, cal.samples.get(id));
    cal.changed.clear();
  }

  async loadCache(slug, platform) {
    const c = this.caches.get(`${slug}|${platform}`);
    if (!c) return null;
    const copy = structuredClone(c);
    if (copy.seen) copy.seen = new Map(copy.seen);
    return copy;
  }

  async saveCache(slug, platform, data) {
    const copy = structuredClone({ ...data, seen: data.seen ? [...data.seen] : undefined });
    if (!data.seen) delete copy.seen;
    this.caches.set(`${slug}|${platform}`, copy);
  }

  async vintedHistories() {
    return [...this.caches].filter(([k]) => k.endsWith('|vinted')).sort(([a], [b]) => (a < b ? -1 : 1))
      .map(([, c]) => ({ query: c.query, seen: new Map(c.seen || []) }));
  }
}

// D1 allows 100 bound values per statement, so multi-row inserts go in chunks.
function inserts(db, sql, cols, rows, verb = 'INSERT') {
  const per = Math.floor(100 / cols);
  const out = [];
  for (let i = 0; i < rows.length; i += per) {
    const chunk = rows.slice(i, i + per);
    out.push(db.prepare(`${verb} ${sql} VALUES ${chunk.map(() => `(${new Array(cols).fill('?').join(', ')})`).join(', ')}`)
      .bind(...chunk.flat()));
  }
  return out;
}

export class D1Store {
  constructor(db) {
    this.db = db;
  }

  async ebayBlock(block) {
    const { results } = await this.db.prepare('SELECT item_id, start_ts FROM ebay_calibration_points WHERE block = ? ORDER BY item_id')
      .bind(block).all();
    return { ids: results.map((r) => r.item_id), ts: results.map((r) => r.start_ts) };
  }

  async ebayAdd(points, source) {
    if (!points.length) return 0;
    const res = await this.db.batch(inserts(this.db, 'INTO ebay_calibration_points (item_id, block, start_ts, source)', 4,
      points.map(([id, block, ts]) => [id, block, ts, source]), 'INSERT OR IGNORE'));
    return res.reduce((s, r) => s + (r.meta?.changes || 0), 0);
  }

  async vintedCalibration() {
    const { results } = await this.db.prepare('SELECT item_id, lo_ts, hi_ts FROM vinted_calibration').all();
    return new Map(results.map((r) => [r.item_id, [r.lo_ts, r.hi_ts]]));
  }

  async saveVintedCalibration(cal) {
    const rows = [...cal.changed].map((id) => [id, ...cal.samples.get(id)]);
    if (rows.length) await this.db.batch(inserts(this.db, 'INTO vinted_calibration (item_id, lo_ts, hi_ts)', 3, rows, 'INSERT OR REPLACE'));
    cal.changed.clear();
  }

  async loadCache(slug, platform) {
    const head = await this.db.prepare('SELECT query, fetched_at, extra_json FROM scan_cache WHERE slug = ? AND platform = ?')
      .bind(slug, platform).first();
    if (!head) return null;
    const [rows, seen] = await this.db.batch([
      this.db.prepare('SELECT row_json FROM scan_cache_rows WHERE slug = ? AND platform = ? ORDER BY n').bind(slug, platform),
      this.db.prepare('SELECT item_id, rec_json FROM vinted_seen WHERE slug = ? ORDER BY n').bind(slug),
    ]);
    const data = { query: head.query, fetched_at: head.fetched_at, ...parseJson(head.extra_json, {}),
      rows: rows.results.map((r) => JSON.parse(r.row_json)) };
    if (platform === 'vinted') data.seen = new Map(seen.results.map((r) => [r.item_id, JSON.parse(r.rec_json)]));
    return data;
  }

  async saveCache(slug, platform, data) {
    const { query, fetched_at: fetchedAt, rows = [], seen, ...extra } = data;
    const stmts = [
      this.db.prepare(`INSERT INTO scan_cache (slug, platform, query, fetched_at, extra_json) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (slug, platform) DO UPDATE SET query = excluded.query, fetched_at = excluded.fetched_at, extra_json = excluded.extra_json`)
        .bind(slug, platform, query, fetchedAt, JSON.stringify(extra)),
      this.db.prepare('DELETE FROM scan_cache_rows WHERE slug = ? AND platform = ?').bind(slug, platform),
      ...inserts(this.db, 'INTO scan_cache_rows (slug, platform, n, row_json)', 4, rows.map((r, n) => [slug, platform, n, JSON.stringify(r)])),
    ];
    if (seen) {
      stmts.push(this.db.prepare('DELETE FROM vinted_seen WHERE slug = ?').bind(slug));
      stmts.push(...inserts(this.db, 'INTO vinted_seen (slug, item_id, first_seen, last_checked, resolved, relevant, rec_json, n)', 8,
        [...seen].map(([iid, rec], n) => [slug, iid, rec.first_seen, rec.last_checked ?? null, rec.resolved ?? null,
          rec.relevant ? 1 : 0, JSON.stringify(rec), n])));
    }
    await this.db.batch(stmts);  // all or nothing
  }

  async vintedHistories() {
    // only what can be due: not resolved, first seen within the last 31 days
    const since = new Date(Date.now() - 31 * 86400000).toISOString().slice(0, 10);
    const { results } = await this.db.prepare(`SELECT s.slug, c.query, s.item_id, s.rec_json FROM vinted_seen s
      JOIN scan_cache c ON c.slug = s.slug AND c.platform = 'vinted'
      WHERE s.resolved IS NULL AND s.relevant = 1 AND s.first_seen >= ? ORDER BY s.slug, s.n`).bind(since).all();
    const out = new Map();
    for (const r of results) {
      if (!out.has(r.slug)) out.set(r.slug, { query: r.query, seen: new Map() });
      out.get(r.slug).seen.set(r.item_id, JSON.parse(r.rec_json));
    }
    return [...out.values()];
  }
}
