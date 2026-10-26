// Scan routes and D1 storage in the Workers runtime. The Vinted history scenario recorded from the
// Python (scripts/make_golden.py) is replayed through D1 here, so every SQL statement is exercised.
import { describe, expect, inject, it } from 'vitest';
import { env, exports } from 'cloudflare:workers';
import { ingest, markRelevant, savedRows } from '../src/scrapers/ingest.js';
import { normaliseRecheckResults, sqlApplyRechecks, sqlDueRechecks, sqlRecheckIds } from '../src/v2.js';
import { D1Store } from '../src/store.js';
import { BrowseAPI } from '../src/scrapers/ebay.js';

const g = inject('history');
const t0 = Date.parse(g.t0);
const H = 3600000;
const call = (path, init) => exports.default.fetch(new Request('https://api.test' + path, init));
const gzip = async (obj) => new Response(new Blob([JSON.stringify(obj)]).stream().pipeThrough(new CompressionStream('gzip'))).arrayBuffer();
const strip = (rows) => rows.map(({ observed_at, ...r }) => r);  // eslint-disable-line no-unused-vars

describe('D1 storage', () => {
  it('replays the Vinted history scenario like the Python, with re-checks done in SQL', async () => {
    const store = new D1Store(env.DB);
    const q = g.item.search_queries;
    const steps = g.steps.slice();
    let [rows, stats] = await ingest(g.scan1, q, { store, env: {} });
    let s = steps.shift();
    expect(stats).toEqual(s.stats);
    expect(rows.length).toBe(s.rows.length);
    await markRelevant(store, q.vinted, steps.shift().ids);
    for (const h of [1, 4]) expect(await sqlDueRechecks(env.DB, 20, t0 + h * H)).toEqual(steps.shift().out);
    expect(await sqlRecheckIds(env.DB, q.vinted, t0 + 8 * H)).toEqual(steps.shift().out);
    [rows, stats] = await ingest(g.scan2, q, { store, env: {} });
    s = steps.shift();
    expect(stats).toEqual(s.stats);
    expect(rows.map((r) => [r.listing_id, r.obs_type])).toEqual(s.rows.map((r) => [r.listing_id, r.obs_type]));
    // the background run: statuses applied in SQL, the saved rows rebuilt when read
    s = steps.shift();
    const [results, refresh] = normaliseRecheckResults(g.background);
    expect({ ...(await sqlApplyRechecks(env.DB, results)), session_refresh: refresh }).toEqual(s.out);
    const saved = await savedRows(store, q.vinted, 'vinted');
    expect(saved.map((r) => [r.listing_id, r.obs_type, r.t_days, r.t_upper_days])).toEqual(
      s.saved.map((r) => [r.listing_id, r.obs_type, r.t_days, r.t_upper_days]));
    for (const h of [16, 23, 30]) expect(await sqlDueRechecks(env.DB, 20, t0 + h * H)).toEqual(steps.shift().out);
  });

  it('keeps eBay calibration points once, however often they arrive', async () => {
    const store = new D1Store(env.DB);
    expect(await store.ebayAdd([[999000000000, '999', 1792900000], [999000000010, '999', 1792900100]], 'test')).toBe(2);
    expect(await store.ebayAdd([[999000000000, '999', 1792900000]], 'test')).toBe(0);
    expect(await store.ebayBlock('999')).toEqual({ ids: [999000000000, 999000000010], ts: [1792900000, 1792900100] });
  });
});

describe('scan routes', () => {
  it('scan-pages: gzipped pages in, a full result out, Vinted listings remembered', async () => {
    const r = await call('/api/v1/scan-pages', { method: 'POST', body: await gzip({ item: g.item, pages: g.scan1 }),
      headers: { 'Content-Type': 'application/json', 'Content-Encoding': 'gzip', Origin: 'https://lewisdn.com' } });
    expect(r.status).toBe(200);
    expect(r.headers.get('Access-Control-Allow-Origin')).toBe('https://lewisdn.com');
    const out = await r.json();
    expect(out.market_summary.data_source).toBe('live');
    expect(out.platform_recommendations.ebay.n_sales).toBeGreaterThan(0);
    expect(new Set(out.comparable_listings.map((x) => x.platform))).toEqual(new Set(['ebay', 'vinted']));
    expect(Object.keys(out.price_curve_grid)).toEqual(['fair', 'good', 'very_good', 'mint']);
    const relevant = await env.DB.prepare('SELECT COUNT(*) AS n FROM vinted_seen WHERE relevant = 1').first();
    expect(relevant.n).toBeGreaterThan(0);
  });

  it('scan-pages: a stopped site and a session-refresh page become clear warnings', async () => {
    const pages = g.scan1.filter((p) => p.platform === 'ebay').concat([{ platform: 'vinted', kind: 'catalog', page: 1,
      url: 'https://www.vinted.co.uk/catalog?search_text=x', final_url: 'https://www.vinted.co.uk/session-refresh?ref_url=%2Fcatalog',
      status: 200, html: '<title>Session refresh</title>', fetched_at: g.scan1[0].fetched_at }]);
    const out = await (await call('/api/v1/scan-pages', { method: 'POST', body: JSON.stringify({ item: g.item, pages,
      stopped: { ebay: { reason: 'sign_in_required' } } }) })).json();
    expect(out.warnings.some((w) => w.includes('signed in'))).toBe(true);
    expect(out.warnings.some((w) => w.includes('1 Vinted page came back as'))).toBe(true);
  });

  it('market-scan: saved data, or the labelled example when there is none', async () => {
    await call('/api/v1/scan-pages', { method: 'POST', body: JSON.stringify({ item: g.item, pages: g.scan1 }) });
    const saved = await (await call('/api/v1/market-scan', { method: 'POST', body: JSON.stringify({ item: g.item, allow_live: false }) })).json();
    expect(saved.market_summary.data_source).toBe('seed');
    expect(saved.comparable_listings.length).toBeGreaterThan(0);
    const none = await (await call('/api/v1/market-scan', { method: 'POST',
      body: JSON.stringify({ item: { ...g.item, search_queries: { ebay: 'nothing saved', vinted: 'nothing saved' } } }) })).json();
    expect(none.warnings).toContain('Showing EXAMPLE data: no listings found for this item');
    expect(none.item_metadata.search_queries.ebay).toBe('nothing saved');
  });

  it('re-check routes answer in the shapes the extension expects', async () => {
    await call('/api/v1/scan-pages', { method: 'POST', body: JSON.stringify({ item: g.item, pages: g.scan1 }) });
    expect(await (await call(`/api/v1/recheck-ids?vinted_query=${encodeURIComponent(g.item.search_queries.vinted)}`)).json())
      .toHaveProperty('ids');
    expect((await call('/api/v1/recheck-ids')).status).toBe(422);
    expect(await (await call('/api/v1/recheck-due?limit=5')).json()).toHaveProperty('items');
    const res = await call('/api/v1/recheck-results', { method: 'POST', body: await gzip({ results: g.background }),
      headers: { 'Content-Encoding': 'gzip' } });
    expect(await res.json()).toMatchObject({ skipped: expect.any(Number), session_refresh: 0 });
  });

  it('rejects a bad item the way FastAPI would', async () => {
    const r = await call('/api/v1/scan-pages', { method: 'POST', body: JSON.stringify({ item: { title: 'x' }, pages: [] }) });
    expect(r.status).toBe(422);
  });
});

describe('eBay Browse API client', () => {
  it('calls fetch the way the Workers runtime allows (no "Illegal invocation")', async () => {
    const seen = [];
    const fakeFetch = async function (url) {  // strict about `this`, like the runtime's fetch
      if (this !== undefined && this !== globalThis) throw new TypeError('Illegal invocation');
      seen.push(String(url));
      return String(url).includes('oauth2')
        ? Response.json({ access_token: 't', expires_in: 7200 })
        : Response.json({ itemSummaries: [{ legacyItemId: '123456789012', title: 'Nike', price: { value: '30.00', currency: 'GBP' },
          itemCreationDate: '2026-10-25T10:00:00.000Z' }] });
    };
    const cards = await new BrowseAPI({ EBAY_CLIENT_ID: 'id-test', EBAY_CLIENT_SECRET: 's' }, fakeFetch).activeCards('nike', 1);
    expect(cards.map((c) => c.item_id)).toEqual(['123456789012']);
    expect(seen.length).toBe(2);
  });
});
