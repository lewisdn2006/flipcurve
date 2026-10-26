// FlipCurve API on Cloudflare: takes over from the FastAPI server on the laptop
// (NO_LAPTOP_PLAN.md). Same routes and JSON as backend/main.py, so neither the page nor the
// helper extension has to change. The engine is in engine/, page reading in scrapers/, storage in store.js.

import MOCK_MARKET_SCAN from '../../../backend/mock/mock_market_scan.json';
import { run as runEngine } from './engine/pipeline.js';
import { PLATFORMS } from './engine/economics.js';
import { D1Cooldowns, Identifier, IdentifyError, prettyModel } from './identify.js';
import { applyBackgroundRechecks, dueRechecks, ingest, loadCacheRows, markRelevant, recheckIds } from './scrapers/ingest.js';
import { marketScanResponse } from './schema.js';
import { D1Store } from './store.js';

import { SCHEMA_VERSION } from './schema.js';
const CONDITION_TIERS = ['fair', 'good', 'very_good', 'mint'];

// Who may call this from a browser: lewisdn.com pages, localhost while developing, and the
// helper extension (it needs CORS here until api.lewisdn.com points at this Worker, because its
// host permissions don't cover workers.dev).
const ALLOWED_ORIGIN = /^(https:\/\/([a-z0-9-]+\.)*lewisdn\.com|http:\/\/(localhost|127\.0\.0\.1)(:\d+)?|chrome-extension:\/\/fdibajhmjjbjcdfafdofkoedbkhbkfcb)$/;

function corsHeaders(request) {
  const origin = request.headers.get('Origin');
  if (!origin || !ALLOWED_ORIGIN.test(origin)) return {};
  return {
    'Access-Control-Allow-Origin': origin,
    'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Content-Encoding',
    'Access-Control-Max-Age': '86400',
    Vary: 'Origin',
  };
}

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });

async function health(request, env) {
  // Python answers {ok, ebay_api}; the counts show the D1 data is there.
  let data = null;
  try {
    data = await env.DB.prepare(
      `SELECT (SELECT COUNT(*) FROM ebay_calibration_points) AS ebay_calibration_points,
              (SELECT COUNT(*) FROM vinted_calibration) AS vinted_calibration,
              (SELECT COUNT(*) FROM scan_cache) AS saved_scans,
              (SELECT COUNT(*) FROM vinted_seen) AS vinted_seen`,
    ).first();
  } catch (e) {
    return json({ ok: false, ebay_api: false, runtime: 'worker', error: 'database: ' + e.message }, 503);
  }
  return json({ ok: true, ebay_api: Boolean(env.EBAY_CLIENT_ID && env.EBAY_CLIENT_SECRET), runtime: 'worker', data });
}

// ---- identify (backend/main.py). The page tries /identify/start first; this Worker has no
// background jobs (and needs none: no 100 s cut-off here), so that 404s and the page uses these.

const identifyResponse = ({ item, source, confidence = null, warnings = [] }) =>
  ({ schema_version: SCHEMA_VERSION, item, source, cleaned_image_url: null, confidence, warnings });

/** Every model failed: an editable blank item so the user can type it in, and why. */
function identifyFallback(source, why) {
  const item = { title: '', brand: null, model_name: null, category: null, size_or_storage: null, era_or_year: null,
    condition_tier: 'good', condition_score: 6.0, detected_flaws: [], search_queries: { ebay: '', vinted: '' },
    is_broken: false, must_have: [], must_not_have: [] };
  return identifyResponse({ item, source, confidence: 0.0, warnings: [`Couldn't identify the photo automatically (${why}). `
    + 'Type what it is and the searches below, or try Identify again later (each model above says when it\'s tried again).'] });
}

const invalid = (field, msg) => json({ detail: [{ loc: ['body', field], msg }] }, 422);  // FastAPI's shape

async function identifyPhotos(request, env) {
  let form;
  try {
    form = await request.formData();
  } catch {
    return invalid('images', 'Expected multipart/form-data with 1-4 images');
  }
  const files = form.getAll('images').filter((f) => typeof f === 'object' && typeof f.arrayBuffer === 'function');
  if (!files.length) return invalid('images', 'Field required');
  const images = await Promise.all(files.slice(0, 4).map(async (f) => new Uint8Array(await f.arrayBuffer())));
  const hint = typeof form.get('hint') === 'string' && form.get('hint').trim() ? form.get('hint') : null;
  try {
    const out = await new Identifier({ env, cooldowns: new D1Cooldowns(env.DB) }).fromImages(images, hint);
    return json(identifyResponse({ item: out.item, source: 'vision', confidence: out.confidence,
      warnings: out.fallback ? [`Identified by ${prettyModel(out.model || '')} (the main model didn't answer)`] : [] }));
  } catch (e) {
    if (e instanceof IdentifyError) return json(identifyFallback('vision', e.reason()));
    console.error(e);
    return json(identifyFallback('vision', `something went wrong: ${e.name}`));
  }
}

async function identifyText(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return invalid('query', 'Expected a JSON body');
  }
  const { query, condition_tier: tier = null } = body || {};
  if (typeof query !== 'string' || query.length < 2) return invalid('query', 'String should have at least 2 characters');
  if (tier !== null && !CONDITION_TIERS.includes(tier)) return invalid('condition_tier', `Input should be ${CONDITION_TIERS.join(', ')}`);
  try {
    const out = await new Identifier({ env, cooldowns: new D1Cooldowns(env.DB) }).fromText(query, tier);
    return json(identifyResponse({ item: out.item, source: 'text', confidence: out.confidence }));
  } catch (e) {
    if (!(e instanceof IdentifyError)) console.error(e);
    // the description itself becomes the search, so the user can carry on
    const data = identifyFallback('text', 'the model is busy or unavailable');
    const q = query.trim().toLowerCase();
    Object.assign(data.item, { title: query.trim(), search_queries: { ebay: q, vinted: q }, condition_tier: tier || 'good' });
    const why = e instanceof IdentifyError ? e.reason() : 'the AI models are busy';
    data.warnings = [`Couldn't look it up automatically (${why}). Your description is used as the search; check it below.`];
    return json(data);
  }
}

// ---- scans (backend/main.py): pages the helper extension collected, or saved data only

const STOP_MESSAGES = {
  sign_in_required: 'eBay needs you signed in to show sold listings. Sign in to eBay in this browser, then scan again.',
  refused: '{site} declined the request for now, so these results use saved data for it.',
  slow: '{site} slowed right down part-way, so this uses the pages that arrived. Scanning again in a few minutes may get more.',
  network_error: "{site} couldn't be reached, so these results use saved data for it.",
  session_expired: "Vinted's session had run out and the helper couldn't renew it, so these results use saved "
    + 'Vinted data. Open vinted.co.uk in this browser, then scan again.',
};
const siteName = (site) => (site === 'ebay' ? 'eBay' : 'Vinted');
const MAX_PAGES = 40;

/** JSON body, gzipped or not (pages are several MB, so the page and the extension gzip them). */
async function jsonBody(request) {
  const raw = new Uint8Array(await request.arrayBuffer());
  const gz = raw[0] === 0x1f && raw[1] === 0x8b;  // the gzip magic, whatever the headers say
  const text = gz ? await new Response(new Blob([raw]).stream().pipeThrough(new DecompressionStream('gzip'))).text()
    : new TextDecoder().decode(raw);
  return JSON.parse(text);
}

/** ItemSpec with its defaults, or an error message. */
function itemSpec(it) {
  if (!it || typeof it !== 'object') return [null, 'item: Field required'];
  if (typeof it.title !== 'string') return [null, 'item.title: Field required'];
  if (!CONDITION_TIERS.includes(it.condition_tier)) return [null, `item.condition_tier: Input should be ${CONDITION_TIERS.join(', ')}`];
  if (typeof it.condition_score !== 'number' || it.condition_score < 0 || it.condition_score > 10) return [null, 'item.condition_score: 0-10 required'];
  const q = it.search_queries;
  if (!q || typeof q.ebay !== 'string' || typeof q.vinted !== 'string') return [null, 'item.search_queries: ebay and vinted required'];
  return [{ title: it.title, brand: it.brand ?? null, model_name: it.model_name ?? null, category: it.category ?? null,
    size_or_storage: it.size_or_storage ?? null, era_or_year: it.era_or_year ?? null, condition_tier: it.condition_tier,
    condition_score: it.condition_score, detected_flaws: it.detected_flaws || [], search_queries: { ebay: q.ebay, vinted: q.vinted },
    is_broken: Boolean(it.is_broken), must_have: it.must_have || [], must_not_have: it.must_not_have || [] }, null];
}

/** The example payload, clearly labelled, so the page never breaks. */
function mockScan(item, warnings) {
  const data = structuredClone(MOCK_MARKET_SCAN);
  data.item_metadata = item;
  data.warnings = warnings.concat(data.warnings || []);
  return marketScanResponse(data);
}

async function scanPages(request, env) {
  let body;
  try {
    body = await jsonBody(request);
  } catch {
    return invalid('body', 'Expected a JSON body (gzipped or not)');
  }
  const [item, bad] = itemSpec(body?.item);
  if (bad) return invalid('item', bad);
  const pages = Array.isArray(body.pages) ? body.pages : [];
  if (pages.length > MAX_PAGES) return invalid('pages', `At most ${MAX_PAGES} pages`);
  const stopped = body.stopped && typeof body.stopped === 'object' ? body.stopped : {};
  const store = new D1Store(env.DB);
  const queries = item.search_queries;
  const warnings = [];
  let [rows, stats] = await ingest(pages, queries, { store, env });
  const have = new Set(rows.map((r) => r.platform));
  if (stats.ebay?.api_error) warnings.push("The eBay API didn't answer this time, so eBay listings come from the helper's pages only.");
  for (const [site, info] of Object.entries(stopped)) {
    warnings.push((STOP_MESSAGES[info?.reason] || '{site} stopped early.').replace('{site}', siteName(site)));
  }
  if (stats.vinted_session_refresh && !('vinted' in stopped)) {
    // helpers before 0.5.0 don't spot the placeholder, so they send it on as if it were listings
    const n = stats.vinted_session_refresh;
    warnings.push(`${n} Vinted page${n !== 1 ? 's' : ''} came back as Vinted's "session refresh" page `
      + 'instead of listings (its session had run out), so they were left out. Helper 0.5.0 renews '
      + 'the session itself; until then, open vinted.co.uk in this browser and scan again.');
  }
  for (const site of ['ebay', 'vinted']) {
    if (have.has(site)) continue;  // nothing fresh from this site: fall back to its saved data
    const cached = queries[site] ? (await loadCacheRows(store, queries[site], site)) || [] : [];
    rows = rows.concat(cached);
    if (cached.length) warnings.push(`Using saved ${siteName(site)} data from an earlier scan.`);
  }
  if (!rows.length) return json(mockScan(item, warnings.concat(['Showing EXAMPLE data: no listings found for this item'])));
  let out;
  try {
    out = runEngine(item, rows, { dataSource: pages.length ? 'live' : 'seed' });
  } catch (e) {
    console.error('engine failed on extension pages', e);
    return json(mockScan(item, warnings.concat(['Engine error on these listings; showing example data'])));
  }
  out.warnings = warnings.concat(out.warnings);
  console.log('scan-pages stats', JSON.stringify(stats));
  if (queries.vinted) {  // background re-checks follow only the listings that matched this item
    await markRelevant(store, queries.vinted, out.comparable_listings
      .filter((r) => r.platform === 'vinted' && !r.is_outlier).map((r) => r.listing_id));
  }
  return json(marketScanResponse(out));
}

/** "Use saved data": the last scan of each search. (There is no live fetching here: the helper
 *  extension does that, through /scan-pages.) */
async function marketScan(request, env) {
  let body;
  try {
    body = await request.json();
  } catch {
    return invalid('body', 'Expected a JSON body');
  }
  const [item, bad] = itemSpec(body?.item);
  if (bad) return invalid('item', bad);
  // the request's platforms, else the Platform enum's order (eBay first) as in MarketScanRequest; it
  // decides ties between platforms (scans use the PLATFORMS order, Vinted first, as the Python does)
  const platforms = (Array.isArray(body.platforms) && body.platforms.length ? body.platforms : ['ebay', 'vinted'])
    .filter((p) => p in PLATFORMS);
  const store = new D1Store(env.DB);
  let rows = [];
  const warnings = [];
  for (const p of platforms) {
    const got = item.search_queries[p] ? await loadCacheRows(store, item.search_queries[p], p) : null;
    if (got) rows = rows.concat(got);
    else warnings.push(`${p}: no listings available`);
  }
  if (rows.length) {
    try {
      const out = runEngine(item, rows, { dataSource: 'seed', platforms });
      out.warnings = warnings.concat(out.warnings);
      return json(marketScanResponse(out));
    } catch (e) {
      console.error('engine failed', e);
      warnings.push('engine error on these listings; showing example data');
    }
  }
  return json(mockScan(item, warnings.concat(['Showing EXAMPLE data: no listings found for this item'])));
}

async function recheckIdsRoute(request, env) {
  const q = new URL(request.url).searchParams.get('vinted_query');
  if (!q) return invalid('vinted_query', 'Field required');
  return json({ ids: await recheckIds(new D1Store(env.DB), q) });
}

async function recheckDue(request, env) {
  const limit = Number(new URL(request.url).searchParams.get('limit') || 20);
  return json({ items: await dueRechecks(new D1Store(env.DB), Math.max(1, Math.min(Number.isFinite(limit) ? limit : 20, 30))) });
}

async function recheckResults(request, env) {
  let body;
  try {
    body = await jsonBody(request);
  } catch {
    return invalid('body', 'Expected a JSON body (gzipped or not)');
  }
  const results = (Array.isArray(body?.results) ? body.results : []).filter((r) => r && typeof r.query === 'string' && r.page?.url);
  const out = await applyBackgroundRechecks(results, { store: new D1Store(env.DB) });
  console.log('background re-checks', JSON.stringify(out));
  return json(out);
}

const ROUTES = {
  'GET /api/v1/health': health,
  'POST /api/v1/identify': identifyPhotos,
  'POST /api/v1/identify-text': identifyText,
  'POST /api/v1/scan-pages': scanPages,
  'POST /api/v1/market-scan': marketScan,
  'GET /api/v1/recheck-ids': recheckIdsRoute,
  'GET /api/v1/recheck-due': recheckDue,
  'POST /api/v1/recheck-results': recheckResults,
};

async function route(request, env, ctx) {
  const url = new URL(request.url);
  if (url.pathname === '/') return Response.redirect('https://lewisdn.com/flipcurve/', 302);
  const handler = ROUTES[`${request.method} ${url.pathname}`];
  if (!handler) return json({ detail: 'Not Found' }, 404);  // FastAPI's wording
  return handler(request, env, ctx);
}

export default {
  async fetch(request, env, ctx) {
    const cors = corsHeaders(request);
    if (request.method === 'OPTIONS') return new Response(null, { status: 204, headers: cors });
    let response;
    try {
      response = await route(request, env, ctx);
    } catch (e) {
      console.error(e);
      response = json({ detail: 'Internal Server Error' }, 500);
    }
    response = new Response(response.body, response);  // redirects come back immutable
    for (const [k, v] of Object.entries(cors)) response.headers.set(k, v);
    return response;
  },
};
