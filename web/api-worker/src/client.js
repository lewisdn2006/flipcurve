// FlipCurve's engine in the browser: the Worker serves this, bundled, as /api/v2/core.js, and the
// page imports it when the server says so (health: client_engine). A scan or "Use saved data" runs
// here with the same code the Worker's /api/v1 routes run (scan.js), against a copy of the saved
// data the Worker sends (/api/v2/state); what the scan changed goes back in one /api/v2/save. That
// keeps every Worker request inside the free plan's 10 ms of CPU.

import { itemSpec, savedResult, scanResult } from './scan.js';
import { blockOf, toCard } from './scrapers/ebay.js';
import { slug } from './scrapers/ingest.js';

// Characters of JSON per saved piece: the Worker's CPU per request grows with the bytes it hands to
// D1, and the free plan allows 10 ms.
const CHUNK = 200000;

/** The saved data a scan needs, in memory, with the same methods as store.js's stores; it remembers
 *  what changed so flush() can send just that. */
export class ClientStore {
  constructor(state) {
    this.points = new Map();     // block -> Map(item id -> start ts)
    for (const [id, ts] of state.ebay_points || []) this.block(blockOf(id)).set(id, ts);
    this.vcal = new Map((state.vinted_calibration || []).map(([id, lo, hi]) => [id, [lo, hi]]));
    this.caches = new Map();
    for (const platform of ['ebay', 'vinted']) {
      const c = state.caches?.[platform];
      if (!c) continue;
      const data = { query: c.head.query, fetched_at: c.head.fetched_at, ...(c.head.extra || {}), rows: c.rows || [] };
      if (platform === 'vinted') data.seen = new Map(c.seen || []);
      this.caches.set(`${slug(c.head.query)}|${platform}`, data);
    }
    this.newPoints = [];
    this.calChanged = new Map();
    this.dirty = new Set();
  }

  block(b) {
    if (!this.points.has(b)) this.points.set(b, new Map());
    return this.points.get(b);
  }

  async ebayBlock(b) {
    const m = this.block(b);
    const ids = [...m.keys()].sort((x, y) => x - y);
    return { ids, ts: ids.map((i) => m.get(i)) };
  }

  async ebayAdd(points, source) {
    let n = 0;
    for (const [id, block, ts] of points) {
      const m = this.block(block);
      if (m.has(id)) continue;
      m.set(id, ts);
      this.newPoints.push([id, block, ts, source]);
      n++;
    }
    return n;
  }

  async vintedCalibration() { return new Map(this.vcal); }

  async saveVintedCalibration(cal) {
    for (const id of cal.changed) {
      this.vcal.set(id, cal.samples.get(id));
      this.calChanged.set(id, cal.samples.get(id));
    }
    cal.changed.clear();
  }

  async loadCache(s, platform) {
    const c = this.caches.get(`${s}|${platform}`);
    return c ? structuredClone(c) : null;
  }

  async saveCache(s, platform, data) {
    this.caches.set(`${s}|${platform}`, structuredClone(data));
    this.dirty.add(`${s}|${platform}`);
  }

  async markRelevant(s, ids) {
    const c = this.caches.get(`${s}|vinted`);
    if (!c?.seen) return;
    const want = new Set(ids);
    for (const [iid, rec] of c.seen) if (want.has(iid)) rec.relevant = true;
    this.dirty.add(`${s}|vinted`);
  }

  /** What changed, as /api/v2/save-part pieces and the /save-commit lines (see v2.js). */
  savePieces() {
    const lines = [], commit = [];
    const chunked = (prefix, items, offsetField = false) => {
      let part = [], size = 0, start = 0;
      const out = () => {
        if (part.length) lines.push(`${prefix}${offsetField ? `\t${start}` : ''}\t[${part.join(',')}]`);
        start += part.length;
        part = [];
        size = 0;
      };
      for (const it of items) {
        const j = JSON.stringify(it);
        if (size + j.length > CHUNK) out();
        part.push(j);
        size += j.length + 1;
      }
      out();
    };
    chunked('ebay_points', this.newPoints);
    chunked('vinted_cal', [...this.calChanged].map(([id, [lo, hi]]) => [id, lo, hi]));
    for (const key of this.dirty) {
      const [s, platform] = key.split('|');
      const { query, fetched_at: _, rows = [], seen, ...extra } = this.caches.get(key);
      commit.push(`cache\t${JSON.stringify({ slug: s, platform, query, extra, seen: Boolean(seen), rows_n: rows.length,
        seen_n: seen ? seen.size : 0 })}`);
      chunked(`rows\t${s}\t${platform}`, rows, true);
      if (seen) chunked(`seen\t${s}`, [...seen], true);
    }
    return { parts: lines, commit };
  }

  /** Send what changed: the pieces (a few at a time), then the commit that puts them in place. */
  async flush(api) {
    const { parts, commit } = this.savePieces();
    if (!parts.length && !commit.length) return;
    const id = crypto.randomUUID();
    const post = (path, body) => call(api, `${path}?id=${id}`, { method: 'POST', headers: { 'Content-Type': 'text/plain;charset=UTF-8' }, body });
    await inBatches(parts.map((line) => () => post('/api/v2/save-part', line)), 4);
    if (commit.length) await post('/api/v2/save-commit', commit.join('\n'));
  }
}

async function call(api, path, init = {}, timeoutMs = 60000) {
  const r = await fetch(api + path, { ...init, signal: AbortSignal.timeout(timeoutMs) });
  if (!r.ok) throw new Error(`${path.split('?')[0]} failed (${r.status}): ${(await r.text()).slice(0, 200)}`);
  return r;
}

/** Run the tasks, `n` at a time. */
async function inBatches(tasks, n) {
  const out = [];
  for (let i = 0; i < tasks.length; i += n) out.push(...await Promise.all(tasks.slice(i, i + n).map((t) => t())));
  return out;
}

/** The saved data for a scan, fetched in pieces (see v2.js: no Worker request carries much). */
async function loadState(api, queries, blocks) {
  const p = new URLSearchParams({ ebay: queries.ebay || '', vinted: queries.vinted || '' });
  const st = await (await call(api, `/api/v2/state?${p}`)).json();
  const getJson = (path) => () => call(api, path).then((r) => r.json());
  const tasks = [], put = [];
  for (let i = 0; i < blocks.length; i += 30) {
    tasks.push(getJson(`/api/v2/points?blocks=${blocks.slice(i, i + 30).join(',')}`));
    put.push(['points']);
  }
  const state = { ebay_points: [], vinted_calibration: st.vinted_calibration, caches: {} };
  for (const pl of ['ebay', 'vinted']) {
    const c = st.caches[pl];
    if (!c) continue;
    state.caches[pl] = { head: c.head, rows: [], seen: pl === 'vinted' ? [] : undefined };
    for (const [kind, n] of [['rows', c.head.rows_n], ...(pl === 'vinted' ? [['seen', c.head.seen_n]] : [])]) {
      for (let from = 0; from < n; from += st.chunk_rows) {
        const q = new URLSearchParams({ kind, slug: c.slug, platform: pl, from: String(from), to: String(Math.min(from + st.chunk_rows, n)) });
        tasks.push(getJson(`/api/v2/chunk?${q}`));
        put.push([pl, kind]);
      }
    }
  }
  const got = await inBatches(tasks, 6);
  got.forEach((part, i) => {
    const [a, kind] = put[i];
    if (a === 'points') state.ebay_points.push(...part);
    else state.caches[a][kind].push(...part);  // pieces come back in order
  });
  return new ClientStore(state);
}

const exampleLoader = (api) => {
  let got = null;
  return () => (got ||= call(api, '/api/v2/example').then((r) => r.json()));
};

/** The eBay Browse API's newest listings for a search, via the Worker (the keys stay there):
 *  [cards, null], [[], error] or [null, null] when the API isn't set up. */
async function ebayApiCards(api, query) {
  let r;
  try {
    r = await fetch(`${api}/api/v2/ebay-active?q=${encodeURIComponent(query)}`, { signal: AbortSignal.timeout(60000) });
  } catch (e) {
    return [[], `${e.name}: ${e.message}`.slice(0, 200)];
  }
  if (r.status === 404) return [null, null];
  if (!r.ok) return [[], ((await r.json().catch(() => ({}))).error || `HTTP ${r.status}`).slice(0, 200)];
  const pages = await r.json();
  return [pages.flatMap((pg) => (pg.itemSummaries || []).map(toCard).filter(Boolean)), null];
}

/**
 * A scan, from the pages the helper extension collected -> MarketScanResponse.
 * ebayApiOn: the server has the eBay API set up (health: ebay_api).
 */
/** The item with its defaults filled in, as the server's routes do (scan.itemSpec). */
function checkedItem(raw) {
  const [item, bad] = itemSpec(raw);
  if (bad) throw new Error(bad);
  return item;
}

export async function scanInBrowser({ api, item: raw, pages, stopped = {}, ebayApiOn = false, onProgress = () => {} }) {
  const item = checkedItem(raw);
  const q = item.search_queries;
  // the eBay calibration blocks the sold listings need (item numbers' first three digits)
  const blocks = new Set();
  for (const p of pages) {
    if (p.platform === 'ebay' && p.kind === 'sold') for (const m of (p.html || '').matchAll(/data-listingid="(\d{12})"/g)) blocks.add(blockOf(m[1]));
  }
  onProgress('Loading saved data…');
  const wantApi = Boolean(ebayApiOn && q.ebay);
  const [store, [apiCards, apiError]] = await Promise.all([loadState(api, q, [...blocks]),
    wantApi ? ebayApiCards(api, q.ebay) : Promise.resolve([null, null])]);
  onProgress('Working out prices and selling times…');
  const [out] = await scanResult({ item, pages, stopped, store, apiCards, apiError, ebayApiOn: wantApi && apiCards !== null,
    mock: exampleLoader(api) });
  onProgress('Saving for next time…');
  try {
    await store.flush(api);
  } catch (e) {
    out.warnings.push(`This scan couldn't be saved for next time (${e.message}).`);
  }
  return out;
}

/** "Use saved data" -> MarketScanResponse. */
export async function savedInBrowser({ api, item: raw, platforms = null }) {
  const item = checkedItem(raw);
  const store = await loadState(api, item.search_queries, []);
  return savedResult({ item, platforms, store, mock: exampleLoader(api) });
}
