// The browser engine (client.js) against this Worker, in-process: state -> scan in the "browser" ->
// save, and the light routes. A scan done that way must give exactly what the same scan gives with all
// the data in memory (scan.js + MemoryStore, which test/node checks against the Python).
import { beforeAll, describe, expect, inject, it } from 'vitest';
import { env, exports } from 'cloudflare:workers';
import { savedInBrowser, scanInBrowser } from '../src/client.js';
import { itemSpec, savedResult, scanResult } from '../src/scan.js';
import { MemoryStore } from '../src/store.js';
import EXAMPLE_TEXT from '../dist/example-text.js';

const g = inject('history');
const API = 'https://api.test';
const call = (path, init) => exports.default.fetch(new Request(API + path, init));
const mock = async () => JSON.parse(EXAMPLE_TEXT);
const noTime = (out) => ({ ...out, market_summary: { ...out.market_summary, fetched_at: null } });

beforeAll(async () => {
  // the "browser" talks to this Worker
  globalThis.fetch = (url, init) => exports.default.fetch(new Request(url, init));
  for (const t of ['ebay_calibration_points', 'vinted_calibration', 'scan_cache', 'scan_cache_rows', 'vinted_seen']) {
    await env.DB.prepare(`DELETE FROM ${t}`).run();
  }
});

describe('scans in the browser', () => {
  it('give the same results as with all the data in memory, scan after scan', async () => {
    const memory = new MemoryStore();
    const [item] = itemSpec(g.item);  // as the routes (and FastAPI) fill in an item's defaults
    for (const pages of [g.scan1, g.scan2]) {
      const want = (await scanResult({ item, pages, store: memory, mock }))[0];
      const got = await scanInBrowser({ api: API, item: g.item, pages });
      expect(noTime(got)).toEqual(noTime(want));
    }
    // and "Use saved data" afterwards
    const want = await savedResult({ item, store: memory, mock });
    expect(noTime(await savedInBrowser({ api: API, item: g.item }))).toEqual(noTime(want));
  });

  it('save and load round-trip a search exactly, in pieces', async () => {
    const q = g.item.search_queries.vinted;
    const st = await (await call(`/api/v2/state?vinted=${encodeURIComponent(q)}`)).json();
    const head = st.caches.vinted.head, s = st.caches.vinted.slug;
    expect(head.rows_n).toBeGreaterThan(0);
    expect(head.seen_n).toBeGreaterThan(0);
    expect(head.extra.pages_at).toMatch(/\+00:00$/);
    const piece = async (kind, n) => {
      const out = [];
      for (let from = 0; from < n; from += st.chunk_rows) {
        out.push(...await (await call(`/api/v2/chunk?kind=${kind}&slug=${s}&platform=vinted&from=${from}&to=${Math.min(from + st.chunk_rows, n)}`)).json());
      }
      return out;
    };
    const rows = await piece('rows', head.rows_n), seen = await piece('seen', head.seen_n);
    expect(rows.length).toBe(head.rows_n);
    // save exactly what was loaded, in two pieces each, then commit: nothing changes
    const id = 'test-save-0001';
    const post = (path, body) => call(`${path}?id=${id}`, { method: 'POST', body });
    const half = (a) => [a.slice(0, Math.ceil(a.length / 2)), a.slice(Math.ceil(a.length / 2))];
    const [r1, r2] = half(rows), [s1, s2] = half(seen);
    for (const line of [`rows\t${s}\tvinted\t0\t${JSON.stringify(r1)}`, `rows\t${s}\tvinted\t${r1.length}\t${JSON.stringify(r2)}`,
      `seen\t${s}\t0\t${JSON.stringify(s1)}`, `seen\t${s}\t${s1.length}\t${JSON.stringify(s2)}`]) {
      expect((await post('/api/v2/save-part', line)).status).toBe(200);
    }
    const commit = `cache\t${JSON.stringify({ slug: s, platform: 'vinted', query: q, extra: head.extra, seen: true, rows_n: rows.length, seen_n: seen.length })}`;
    expect(await (await post('/api/v2/save-commit', commit)).json()).toMatchObject({ ok: true, caches: 1 });
    expect(await piece('rows', head.rows_n)).toEqual(rows);
    expect(await piece('seen', head.seen_n)).toEqual(seen);
    const left = await env.DB.prepare('SELECT (SELECT COUNT(*) FROM stage_rows) + (SELECT COUNT(*) FROM stage_seen) AS n').first();
    expect(left.n).toBe(0);  // staging cleared
  });

  it('refuses a save with a piece missing, and changes nothing', async () => {
    const q = g.item.search_queries.vinted;
    const before = await (await call(`/api/v2/state?vinted=${encodeURIComponent(q)}`)).json();
    const s = before.caches.vinted.slug;
    const id = 'test-save-0002';
    await call(`/api/v2/save-part?id=${id}`, { method: 'POST', body: `rows\t${s}\tvinted\t0\t[{"listing_id":"x"}]` });
    const r = await call(`/api/v2/save-commit?id=${id}`, { method: 'POST',
      body: `cache\t${JSON.stringify({ slug: s, platform: 'vinted', query: q, extra: {}, seen: false, rows_n: 2 })}` });
    expect(r.status).toBe(409);
    const after = await (await call(`/api/v2/state?vinted=${encodeURIComponent(q)}`)).json();
    expect(after.caches.vinted.head).toEqual(before.caches.vinted.head);
  });

  it('rejects malformed pieces', async () => {
    for (const [path, body] of [['/api/v2/save-part?id=test-save-0003', 'nonsense\t[]'],
      ['/api/v2/save-part?id=test-save-0003', 'rows\tBAD SLUG\tvinted\t0\t[]'],
      ['/api/v2/save-part?id=x', 'rows\tok\tvinted\t0\t[]'],
      ['/api/v2/save-commit?id=test-save-0003', 'cache\t{"slug":"x","platform":"amazon","query":"x","rows_n":0}']]) {
      expect((await call(path, { method: 'POST', body })).status, body).toBe(422);
    }
    expect((await call('/api/v2/chunk?kind=rows&slug=x&platform=vinted&from=0&to=999')).status).toBe(422);
    expect((await call(`/api/v2/points?blocks=${Array.from({ length: 31 }, (_, i) => 100 + i).join(',')}`)).status).toBe(422);
  });
});

describe('light routes', () => {
  it('serve the engine with a versioned, cacheable URL', async () => {
    const h = await (await call('/api/v1/health')).json();
    expect(h.client_engine).toMatch(/^\/api\/v2\/core\.js\?v=[0-9a-f]{12}$/);
    const r = await call(h.client_engine, { headers: { Origin: 'https://lewisdn.com' } });
    expect(r.headers.get('Content-Type')).toContain('javascript');
    expect(r.headers.get('Access-Control-Allow-Origin')).toBe('https://lewisdn.com');
    expect((await r.text()).length).toBeGreaterThan(100000);
  });

  it('the example payload', async () => {
    expect((await (await call('/api/v2/example')).json()).schema_version).toBe('1.1');
  });

  it('eBay pass-through says when the API is not set up', async () => {
    expect((await call('/api/v2/ebay-active?q=nike')).status).toBe(404);  // no keys in tests
  });

  it('background re-checks: statuses from helper 0.6.0, pages from older helpers', async () => {
    const q = g.item.search_queries.vinted;
    const st = await (await call(`/api/v2/state?vinted=${encodeURIComponent(q)}`)).json();
    const seen = await (await call(`/api/v2/chunk?kind=seen&slug=${st.caches.vinted.slug}&from=0&to=2`)).json();
    const [a, b] = seen.map(([iid]) => iid);
    const r = await call('/api/v1/recheck-results', { method: 'POST', body: JSON.stringify({ results: [
      { query: q, id: a, status: 'sold', fetched_at: '2026-10-25T20:00:00.000Z' },
      { query: q, id: 'not-seen', status: 'active', fetched_at: '2026-10-25T20:00:00.000Z' },
      { query: q, page: { platform: 'vinted', kind: 'recheck', url: `https://www.vinted.co.uk/items/${b}`, status: 200,
        html: '<script type="application/ld+json">{"offers":{"availability":"InStock"}}</script><button data-testid="item-buy-button">Buy</button>',
        fetched_at: '2026-10-25T20:00:00.000Z' } },
      { query: q, page: { platform: 'vinted', kind: 'recheck', url: `https://www.vinted.co.uk/items/${b}`, status: 200,
        final_url: 'https://www.vinted.co.uk/session-refresh?ref_url=x', html: '', fetched_at: '2026-10-25T20:00:00.000Z' } },
    ] }) });
    expect(await r.json()).toEqual({ active: 1, sold: 1, deleted: 0, unknown: 0, skipped: 1, session_refresh: 1 });
    const rec = await env.DB.prepare('SELECT resolved, rec_json FROM vinted_seen WHERE item_id = ?').bind(a).first();
    expect(rec.resolved).toBe('sold');
    expect(JSON.parse(rec.rec_json)).toMatchObject({ resolved: 'sold', sold_seen_at: '2026-10-25T20:00:00+00:00' });
  });

  it('identify with base64 photos answers like the multipart route', async () => {
    const r = await call('/api/v2/identify', { method: 'POST', body: JSON.stringify({ images: [{ data: '/9j/4AAQSkZJRgABAQ==', mime_type: 'image/jpeg' }] }) });
    const out = await r.json();
    expect(out.source).toBe('vision');
    expect(out.warnings[0]).toContain('GEMINI_API_KEY is not set');  // no keys in tests
  });
});
