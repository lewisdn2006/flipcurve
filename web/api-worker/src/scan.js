// A scan and "Use saved data" (backend/main.py's _scan_pages and market_scan), shared by the
// Worker's /api/v1 routes and the browser (client.js): the same code, with storage passed in (D1 in
// the Worker, the page's copy of it in the browser) and the example payload fetched when needed.

import { PLATFORMS } from './engine/economics.js';
import { run as runEngine } from './engine/pipeline.js';
import { ingest, markRelevant, savedRows } from './scrapers/ingest.js';
import { marketScanResponse } from './schema.js';

export const CONDITION_TIERS = ['fair', 'good', 'very_good', 'mint'];

export const STOP_MESSAGES = {
  sign_in_required: 'eBay needs you signed in to show sold listings. Sign in to eBay in this browser, then scan again.',
  refused: '{site} declined the request for now, so these results use saved data for it.',
  slow: '{site} slowed right down part-way, so this uses the pages that arrived. Scanning again in a few minutes may get more.',
  network_error: "{site} couldn't be reached, so these results use saved data for it.",
  session_expired: "Vinted's session had run out and the helper couldn't renew it, so these results use saved "
    + 'Vinted data. Open vinted.co.uk in this browser, then scan again.',
};
const siteName = (site) => (site === 'ebay' ? 'eBay' : 'Vinted');

/** ItemSpec with its defaults, or [null, an error message]. */
export function itemSpec(it) {
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

/** The example payload, clearly labelled, so the page never breaks. mock: async () => the example. */
async function example(mock, item, warnings) {
  const data = structuredClone(await mock());
  data.item_metadata = item;
  data.warnings = warnings.concat(data.warnings || []);
  return marketScanResponse(data);
}

/**
 * Pages the helper extension collected -> [MarketScanResponse, stats].
 * ctx: {store, env?, apiCards?, apiError?, ebayApiOn?} as for ingest; mock: async () => the example.
 */
export async function scanResult({ item, pages, stopped = {}, mock, ...ctx }) {
  const queries = item.search_queries;
  const warnings = [];
  let [rows, stats] = await ingest(pages, queries, ctx);
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
    const cached = queries[site] ? (await savedRows(ctx.store, queries[site], site)) || [] : [];
    rows = rows.concat(cached);
    if (cached.length) warnings.push(`Using saved ${siteName(site)} data from an earlier scan.`);
  }
  if (!rows.length) return [await example(mock, item, warnings.concat(['Showing EXAMPLE data: no listings found for this item'])), stats];
  let out;
  try {
    out = runEngine(item, rows, { dataSource: pages.length ? 'live' : 'seed' });
  } catch (e) {
    console.error('engine failed on extension pages', e);
    return [await example(mock, item, warnings.concat(['Engine error on these listings; showing example data'])), stats];
  }
  out.warnings = warnings.concat(out.warnings);
  if (queries.vinted) {  // background re-checks follow only the listings that matched this item
    await markRelevant(ctx.store, queries.vinted, out.comparable_listings
      .filter((r) => r.platform === 'vinted' && !r.is_outlier).map((r) => r.listing_id));
  }
  return [marketScanResponse(out), stats];
}

/** "Use saved data": the last scan of each search. platforms: the request's, else the Platform enum's
 *  order (eBay first) as in MarketScanRequest; it decides ties between platforms. */
export async function savedResult({ item, platforms = null, store, mock }) {
  platforms = (Array.isArray(platforms) && platforms.length ? platforms : ['ebay', 'vinted']).filter((p) => p in PLATFORMS);
  let rows = [];
  const warnings = [];
  for (const p of platforms) {
    const got = item.search_queries[p] ? await savedRows(store, item.search_queries[p], p) : null;
    if (got) rows = rows.concat(got);
    else warnings.push(`${p}: no listings available`);
  }
  if (rows.length) {
    try {
      const out = runEngine(item, rows, { dataSource: 'seed', platforms });
      out.warnings = warnings.concat(out.warnings);
      return marketScanResponse(out);
    } catch (e) {
      console.error('engine failed', e);
      warnings.push('engine error on these listings; showing example data');
    }
  }
  return example(mock, item, warnings.concat(['Showing EXAMPLE data: no listings found for this item']));
}
