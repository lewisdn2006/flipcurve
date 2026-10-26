// The Worker's side when the browser does the work (client.js), kept inside the free plan's 10 ms of
// CPU per request: it only moves text between the page and D1. D1's JSON functions build and unpack
// the big JSON (json_group_array, json_each), so the Worker never parses listings or history itself.
//
//   GET  /api/v2/core.js?v=...   the browser engine (dist/core.js, built from client.js)
//   GET  /api/v2/example         the example payload shown when there are no listings
//   GET  /api/v2/state           saved data a scan needs: Vinted calibration, the searches and their sizes
//   GET  /api/v2/chunk           a piece of a search's listings or history
//   GET  /api/v2/points          eBay calibration for some blocks
//   POST /api/v2/save-part       a piece of what a scan changed; POST /api/v2/save-commit puts it in place
//   GET  /api/v2/ebay-active     the eBay Browse API's newest listings, passed through unparsed
// and, for the page and the helper extension, the /api/v1 re-check routes done in SQL.

import { CORE, CORE_VERSION } from '../dist/core-text.js';
import EXAMPLE from '../dist/example-text.js';
import { BrowseAPI, ebayApiConfigured } from './scrapers/ebay.js';
import { classifyItemPage } from './scrapers/classify.js';
import { iidOf, slug } from './scrapers/ingest.js';
import { isSessionRefresh } from './scrapers/vinted.js';
import { isoformat, parseTime } from './time.js';

const JSON_HEADERS = { 'Content-Type': 'application/json' };
const jsonText = (text, status = 200) => new Response(text, { status, headers: JSON_HEADERS });
const json = (data, status = 200) => jsonText(JSON.stringify(data), status);
const bad = (msg) => json({ detail: msg }, 422);

export const coreUrl = () => `/api/v2/core.js?v=${CORE_VERSION}`;

export function serveCore() {
  return new Response(CORE, { headers: { 'Content-Type': 'text/javascript; charset=utf-8',
    'Cache-Control': 'public, max-age=31536000, immutable' } });  // the URL changes with every build
}

export const serveExample = () => new Response(EXAMPLE, { headers: { ...JSON_HEADERS, 'Cache-Control': 'public, max-age=3600' } });

const SLUG = /^[a-z0-9-]{1,200}$/;
const PLATFORMS = ['ebay', 'vinted'];

// ------------------------------------------------------------------ state for a scan
// Loading is split so no request carries much: Worker CPU grows with the bytes D1 hands back (about
// 13 ms per MB), and the free plan allows 10 ms. /state gives the small things and the counts, then
// the page fetches the listings and history in parallel pieces (/chunk) and calibration by block (/points).

export const CHUNK_ROWS = 250;

export async function state(request, env) {
  const p = new URL(request.url).searchParams;
  const queries = { ebay: p.get('ebay') || '', vinted: p.get('vinted') || '' };
  const db = env.DB;
  const stmts = [db.prepare('SELECT json_group_array(json_array(item_id, lo_ts, hi_ts) ORDER BY item_id) AS j FROM vinted_calibration')];
  const which = [];
  for (const pl of PLATFORMS) {
    if (!queries[pl]) continue;
    const s = slug(queries[pl]);
    which.push([pl, s]);
    stmts.push(db.prepare(`SELECT json_object('query', query, 'fetched_at', fetched_at, 'extra', json(extra_json),
        'rows_n', (SELECT COUNT(*) FROM scan_cache_rows r WHERE r.slug = c.slug AND r.platform = c.platform),
        'seen_n', (SELECT COUNT(*) FROM vinted_seen v WHERE v.slug = c.slug)) AS j
      FROM scan_cache c WHERE slug = ? AND platform = ?`).bind(s, pl));
  }
  const res = await db.batch(stmts);
  const caches = { ebay: 'null', vinted: 'null' };
  which.forEach(([pl, s], i) => {
    const head = res[i + 1].results[0]?.j;
    if (head) caches[pl] = `{"slug":${JSON.stringify(s)},"head":${head}}`;
  });
  return jsonText(`{"vinted_calibration":${res[0].results[0]?.j || '[]'},"chunk_rows":${CHUNK_ROWS},`
    + `"caches":{"ebay":${caches.ebay},"vinted":${caches.vinted}}}`);
}

/** One piece of a saved search: kind=rows (slug, platform) or kind=seen (slug), from <= n < to. */
export async function chunk(request, env) {
  const p = new URL(request.url).searchParams;
  const s = p.get('slug') || '', from = Number(p.get('from')), to = Number(p.get('to'));
  if (!SLUG.test(s) || !Number.isInteger(from) || !Number.isInteger(to) || to <= from || to - from > CHUNK_ROWS) return bad('bad slug or range');
  let stmt;
  if (p.get('kind') === 'rows' && PLATFORMS.includes(p.get('platform'))) {
    stmt = env.DB.prepare(`SELECT json_group_array(json(row_json) ORDER BY n) AS j FROM scan_cache_rows
      WHERE slug = ? AND platform = ? AND n >= ? AND n < ?`).bind(s, p.get('platform'), from, to);
  } else if (p.get('kind') === 'seen') {
    stmt = env.DB.prepare(`SELECT json_group_array(json_array(item_id, json(rec_json)) ORDER BY n) AS j FROM vinted_seen
      WHERE slug = ? AND n >= ? AND n < ?`).bind(s, from, to);
  } else return bad('kind: rows (with platform) or seen');
  return jsonText((await stmt.first())?.j || '[]');
}

/** eBay calibration points for some blocks (item numbers' first three digits), up to 30 at a time. */
export async function points(request, env) {
  const blocks = [...new Set((new URL(request.url).searchParams.get('blocks') || '').split(',').filter((b) => /^\d{3}$/.test(b)))];
  if (blocks.length > 30) return bad('at most 30 blocks');
  if (!blocks.length) return jsonText('[]');
  const row = await env.DB.prepare(`SELECT json_group_array(json_array(item_id, start_ts) ORDER BY item_id) AS j
    FROM ebay_calibration_points WHERE block IN (${blocks.map(() => '?').join(',')})`).bind(...blocks).first();
  return jsonText(row?.j || '[]');
}

// ------------------------------------------------------------------ saving what a scan changed
// In pieces too: POST /save-part?id=... once per piece, then POST /save-commit?id=... which moves the
// staged listings and history into place in one batch (all or nothing). Each piece is one line:
//    rows \t slug \t platform \t offset \t [row, ...]
//    seen \t slug \t offset \t [[item id, record], ...]
//    ebay_points \t [[item id, block, start ts, source], ...]     (kept at once: adding is idempotent)
//    vinted_cal \t [[item id, lo, hi], ...]                         (likewise)
// and the commit's body has one line per search replaced:
//    cache \t {slug, platform, query, extra, seen: bool, rows_n, seen_n}
// The counts let the commit refuse a save with a piece missing. The saved time is the Worker's.

const SAVE_ID = /^[a-z0-9-]{8,64}$/;
const MAX_PART = 1.5e6;

export async function savePart(request, env) {
  const id = new URL(request.url).searchParams.get('id') || '';
  if (!SAVE_ID.test(id)) return bad('id: 8-64 of a-z 0-9 -');
  const line = await request.text();
  if (line.length > MAX_PART) return json({ detail: 'Too large: send smaller pieces' }, 413);
  const db = env.DB;
  const at = Math.floor(Date.now() / 1000);
  let pos = 0;
  const field = () => { const t = line.indexOf('\t', pos); const v = line.slice(pos, t < 0 ? undefined : t); pos = t < 0 ? line.length : t + 1; return v; };
  const kind = field();
  let stmt;
  if (kind === 'rows') {
    const [s, pl, off] = [field(), field(), Number(field())];
    if (!SLUG.test(s) || !PLATFORMS.includes(pl) || !Number.isInteger(off)) return bad('rows: bad slug, platform or offset');
    stmt = db.prepare(`INSERT INTO stage_rows (save_id, slug, platform, n, row_json, created_at)
      SELECT ?1, ?2, ?3, ?4 + key, value, ?6 FROM json_each(?5)`).bind(id, s, pl, off, line.slice(pos), at);
  } else if (kind === 'seen') {
    const [s, off] = [field(), Number(field())];
    if (!SLUG.test(s) || !Number.isInteger(off)) return bad('seen: bad slug or offset');
    stmt = db.prepare(`INSERT INTO stage_seen (save_id, slug, item_id, first_seen, last_checked, resolved, relevant, rec_json, n, created_at)
      SELECT ?1, ?2, json_extract(value, '$[0]'), json_extract(value, '$[1].first_seen'), json_extract(value, '$[1].last_checked'),
        json_extract(value, '$[1].resolved'), CASE WHEN json_extract(value, '$[1].relevant') THEN 1 ELSE 0 END,
        json_extract(value, '$[1]'), ?3 + key, ?5
      FROM json_each(?4)`).bind(id, s, off, line.slice(pos), at);
  } else if (kind === 'ebay_points') {
    stmt = db.prepare(`INSERT OR IGNORE INTO ebay_calibration_points (item_id, block, start_ts, source)
      SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), json_extract(value, '$[2]'), json_extract(value, '$[3]')
      FROM json_each(?)`).bind(line.slice(pos));
  } else if (kind === 'vinted_cal') {
    stmt = db.prepare(`INSERT OR REPLACE INTO vinted_calibration (item_id, lo_ts, hi_ts)
      SELECT json_extract(value, '$[0]'), json_extract(value, '$[1]'), json_extract(value, '$[2]') FROM json_each(?)`).bind(line.slice(pos));
  } else {
    return bad(`unknown piece: ${kind.slice(0, 20)}`);
  }
  await stmt.run();
  return json({ ok: true });
}

export async function saveCommit(request, env) {
  const id = new URL(request.url).searchParams.get('id') || '';
  if (!SAVE_ID.test(id)) return bad('id: 8-64 of a-z 0-9 -');
  const text = await request.text();
  if (text.length > 100000) return json({ detail: 'Too large' }, 413);
  const metas = [];
  for (const line of text.split('\n')) {
    if (!line) continue;
    if (!line.startsWith('cache\t')) return bad('commit: only cache lines');
    let m;
    try { m = JSON.parse(line.slice(6)); } catch { return bad('cache: not JSON'); }
    if (!SLUG.test(m.slug) || !PLATFORMS.includes(m.platform) || typeof m.query !== 'string'
      || !Number.isInteger(m.rows_n) || (m.seen && !Number.isInteger(m.seen_n))) return bad('cache: bad slug, platform, query or counts');
    metas.push(m);
  }
  const db = env.DB;
  // every piece must have arrived
  const counts = await db.batch(metas.flatMap((m) => [
    db.prepare('SELECT COUNT(*) AS n FROM stage_rows WHERE save_id = ? AND slug = ? AND platform = ?').bind(id, m.slug, m.platform),
    db.prepare('SELECT COUNT(*) AS n FROM stage_seen WHERE save_id = ? AND slug = ?').bind(id, m.slug),
  ]));
  for (const [i, m] of metas.entries()) {
    const rowsN = counts[2 * i].results[0].n, seenN = counts[2 * i + 1].results[0].n;
    if (rowsN !== m.rows_n || (m.seen && seenN !== m.seen_n)) {
      return json({ detail: `Save incomplete for ${m.slug} ${m.platform}: ${rowsN}/${m.rows_n} rows, ${seenN}/${m.seen_n ?? 0} history` }, 409);
    }
  }
  const now = isoformat(Date.now());
  const stmts = [];
  for (const m of metas) {
    stmts.push(db.prepare(`INSERT INTO scan_cache (slug, platform, query, fetched_at, extra_json) VALUES (?, ?, ?, ?, ?)
      ON CONFLICT (slug, platform) DO UPDATE SET query = excluded.query, fetched_at = excluded.fetched_at, extra_json = excluded.extra_json`)
      .bind(m.slug, m.platform, m.query, now, JSON.stringify(m.extra || {})));
    stmts.push(db.prepare('DELETE FROM scan_cache_rows WHERE slug = ? AND platform = ?').bind(m.slug, m.platform));
    stmts.push(db.prepare(`INSERT INTO scan_cache_rows (slug, platform, n, row_json)
      SELECT slug, platform, n, row_json FROM stage_rows WHERE save_id = ? AND slug = ? AND platform = ?`).bind(id, m.slug, m.platform));
    if (m.seen) {
      stmts.push(db.prepare('DELETE FROM vinted_seen WHERE slug = ?').bind(m.slug));
      stmts.push(db.prepare(`INSERT INTO vinted_seen (slug, item_id, first_seen, last_checked, resolved, relevant, rec_json, n)
        SELECT slug, item_id, first_seen, last_checked, resolved, relevant, rec_json, n FROM stage_seen WHERE save_id = ? AND slug = ?`)
        .bind(id, m.slug));
    }
  }
  const stale = Math.floor(Date.now() / 1000) - 86400;
  stmts.push(db.prepare('DELETE FROM stage_rows WHERE save_id = ? OR created_at < ?').bind(id, stale));
  stmts.push(db.prepare('DELETE FROM stage_seen WHERE save_id = ? OR created_at < ?').bind(id, stale));
  await db.batch(stmts);
  return json({ ok: true, saved_at: now, caches: metas.length });
}

// ------------------------------------------------------------------ eBay Browse API, passed through

/** The newest UK listings for a search, as the API's own JSON pages ([page, ...]): up to 5 pages of
 *  200, as ingest's ebayRows asked for. The browser turns them into cards. */
export async function ebayActive(request, env) {
  if (!ebayApiConfigured(env)) return json({ error: 'not configured' }, 404);
  const q = new URL(request.url).searchParams.get('q');
  if (!q) return bad('q: Field required');
  const api = new BrowseAPI(env);
  try {
    const first = await api.searchText(q, 0);
    const total = Number((first.slice(0, 4000).match(/"total"\s*:\s*(\d+)/) || [])[1] || 0);
    const pages = Math.min(5, Math.max(1, Math.ceil(total / 200)));
    const rest = await Promise.all(Array.from({ length: pages - 1 }, (_, i) => api.searchText(q, (i + 1) * 200)));
    return jsonText(`[${[first, ...rest].join(',')}]`);
  } catch (e) {
    return json({ error: `${e.name}: ${e.message}`.slice(0, 200) }, 502);
  }
}

// ------------------------------------------------------------------ re-checks, in SQL
// The same rules as ingest.js's due() (page_ingest._due): not resolved; first seen 3 h to 30 days ago;
// not checked in the last 6 hours; never-checked first, then the longest since a check. Times are
// compared as text, which works because they're all isoformat() in UTC.

const H = 3600000;
const DUE_WHERE = `resolved IS NULL AND first_seen <= ?1 AND first_seen >= ?2 AND (last_checked IS NULL OR last_checked <= ?3)`;
const dueBounds = (now) => [isoformat(now - 3 * H), isoformat(now - 30 * 24 * H), isoformat(now - 6 * H)];

/** Vinted listings from earlier scans of this search worth re-checking during this scan (up to 6):
 *  the ones that matched the item, else any. */
export async function sqlRecheckIds(db, query, now = Date.now()) {
  const order = ` ORDER BY COALESCE(last_checked, ''), first_seen, item_id LIMIT 6`;
  const [rel, any] = await db.batch([
    db.prepare(`SELECT item_id FROM vinted_seen WHERE slug = ?4 AND ${DUE_WHERE} AND relevant = 1${order}`).bind(...dueBounds(now), slug(query)),
    db.prepare(`SELECT item_id FROM vinted_seen WHERE slug = ?4 AND ${DUE_WHERE}${order}`).bind(...dueBounds(now), slug(query)),
  ]);
  return (rel.results.length ? rel.results : any.results).map((r) => r.item_id);
}

/** Across every search: listings the extension should re-check in the background (relevant ones). */
export async function sqlDueRechecks(db, limit = 20, now = Date.now()) {
  const { results } = await db.prepare(`SELECT s.item_id AS id, c.query AS query FROM vinted_seen s
    JOIN scan_cache c ON c.slug = s.slug AND c.platform = 'vinted'
    WHERE ${DUE_WHERE.replaceAll(/\b(resolved|first_seen|last_checked)\b/g, 's.$1')} AND s.relevant = 1
    ORDER BY COALESCE(s.last_checked, ''), s.first_seen, c.query, s.item_id LIMIT ?4`).bind(...dueBounds(now), limit).all();
  return results.map((r) => ({ id: r.id, query: r.query }));
}

/** Background re-check results -> history updates (ingest.applyRecheck, in SQL). results:
 *  [{query, id, status, when}] with status active | sold | deleted | unknown and when an isoformat time. */
export async function sqlApplyRechecks(db, results) {
  const out = { active: 0, sold: 0, deleted: 0, unknown: 0, skipped: 0 };
  if (!results.length) return out;
  const res = await db.batch(results.map((r) => db.prepare(`UPDATE vinted_seen SET last_checked = ?1,
      resolved = CASE ?2 WHEN 'sold' THEN 'sold' WHEN 'deleted' THEN 'deleted' ELSE resolved END,
      rec_json = CASE ?2
        WHEN 'sold' THEN json_set(rec_json, '$.last_checked', ?1, '$.resolved', 'sold', '$.sold_seen_at', ?1)
        WHEN 'active' THEN json_set(rec_json, '$.last_checked', ?1, '$.last_active', ?1)
        WHEN 'deleted' THEN json_set(rec_json, '$.last_checked', ?1, '$.resolved', 'deleted')
        ELSE json_set(rec_json, '$.last_checked', ?1) END
    WHERE slug = ?3 AND item_id = ?4 RETURNING item_id`).bind(r.when, r.status, slug(r.query), String(r.id))));
  results.forEach((r, i) => { if (res[i].results.length) out[r.status]++; else out.skipped++; });
  return out;
}

const STATUSES = ['active', 'sold', 'deleted', 'unknown'];

/** The extension's background run. Helper 0.6.0+ sends {query, id, status, fetched_at}; older ones send
 *  the page ({query, page}), whose status is read here with the cheap check (classify.js). */
export function normaliseRecheckResults(raw) {
  const results = [];
  let sessionRefresh = 0;
  for (const r of Array.isArray(raw) ? raw : []) {
    if (!r || typeof r.query !== 'string') continue;
    if (r.page?.url) {
      // placeholders say nothing about the listing: leave it due for the next run
      if (isSessionRefresh(r.page)) { sessionRefresh++; continue; }
      results.push({ query: r.query, id: iidOf(r.page.url), status: classifyItemPage(r.page.html, r.page.status ?? 200),
        when: isoformat(parseTime(r.page.fetched_at) ?? Date.now()) });
    } else if (r.id != null && STATUSES.includes(r.status)) {
      results.push({ query: r.query, id: String(r.id), status: r.status, when: isoformat(parseTime(r.fetched_at) ?? Date.now()) });
    }
  }
  return [results, sessionRefresh];
}
