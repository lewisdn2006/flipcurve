// Pages collected by the helper extension -> ListingRows, and the Vinted re-check history.
// Port of backend/scrapers/page_ingest.py (plus load/save_cache from orchestrator.py).
//
// Each page is {platform, kind, page, url, final_url, status, html, fetched_at}:
//   eBay    kind 'active' (newest-listed, has start times) / 'sold' (recently sold)
//   Vinted  kind 'catalog' (newest listings) / 'item' (one listing, for its upload age)
//           / 'recheck' (a listing seen in an earlier scan: sold since, or still active?)
// Every scan also improves the shared calibrations. Storage (D1 in the Worker, memory in tests) is
// `store`: see store.js.

import { pyRound } from '../engine/numeric.js';
import { DAY_MS, isoFromTs, isoformat, parseTime } from '../time.js';
import { BrowseAPI, EbayCalibration, buildRows, ebayApiConfigured, parseCards } from './ebay.js';
import { VintedCalibration, activeRows as vintedActiveRows, isSessionRefresh, parseCatalog, parseItemPage, vintedRow } from './vinted.js';

const API_PAGES = 5;  // eBay Browse API: up to 1,000 newest UK listings per scan (200 a call)

/** orchestrator.slug: the cache key for a search. */
export const slug = (q) => q.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');

const pageTime = (p) => parseTime(p.fetched_at) ?? Date.now();
const latest = (pages) => (pages.length ? Math.max(...pages.map(pageTime)) : Date.now());
/** Python's {c["item_id"]: c for c in cards}.values(): first position, last value. */
const dedupe = (cards) => [...new Map(cards.map((c) => [c.item_id, c])).values()];

export async function loadCacheRows(store, query, platform) {
  const data = await store.loadCache(slug(query), platform);
  return data && data.rows && data.rows.length ? data.rows : null;
}

const saveCache = (store, query, platform, rows, extra = {}) =>
  store.saveCache(slug(query), platform, { query, fetched_at: isoformat(Date.now()), rows, ...extra });

/** eBay rows from the helper's pages, plus (when EBAY_CLIENT_ID/SECRET are set) up to 1,000 active
 *  listings from the official Browse API, which also become calibration points. */
export async function ebayRows(pages, query, { store, env = {}, fetchFn = fetch }) {
  const cal = new EbayCalibration(store);
  let act = [], sold = [];
  const now = latest(pages);
  let actComplete = false, soldComplete = false, apiCards = [], apiError = null;
  if (ebayApiConfigured(env)) {
    try {
      apiCards = await new BrowseAPI(env, fetchFn).activeCards(query, API_PAGES);
      await cal.add(apiCards.filter((c) => c.created_time != null).map((c) => [c.item_id, c.created_time]), `api:${query}`);
    } catch (e) {  // the helper's pages still work without it
      apiError = `${e.name}: ${e.message}`.slice(0, 200);
    }
  }
  const ordered = pages.slice().sort((a, b) => (a.kind < b.kind ? -1 : a.kind > b.kind ? 1 : (a.page || 0) - (b.page || 0)));
  for (const p of ordered) {
    const cards = parseCards(p.html, pageTime(p));
    if (p.kind === 'active') {
      act.push(...cards);
      await cal.add(cards.filter((c) => c.start_time != null).map((c) => [c.item_id, c.start_time]), `ext:${query}`);
      actComplete = actComplete || cards.length < 200;
    } else if (p.kind === 'sold') {
      sold.push(...cards);
      soldComplete = soldComplete || cards.length < 200;
    }
  }
  if (apiCards.length) {  // the API's exact dates win; helper pages fill any gaps
    act = act.concat(apiCards);
    actComplete = actComplete || apiCards.length < 200 * API_PAGES;
  }
  act = dedupe(act);
  sold = dedupe(sold);
  if (!act.length && !sold.length) return [[], { active: 0, sold: 0, api_error: apiError }];
  const [rows, stats] = await buildRows(act, sold, cal, now, actComplete, soldComplete,
    sold.length || !act.length ? null : 'sold pages missing');
  Object.assign(stats, { api_active: apiCards.length, api_error: apiError });
  await saveCache(store, query, 'ebay', rows, { stats, source: 'extension' });
  return [rows, stats];
}

// ------------------------------------------------------------------ Vinted re-check history
// Every Vinted listing a scan sees is remembered with that search under "seen":
//   {first_seen, card, relevant?, last_checked?, last_active?, resolved?: sold|deleted, sold_seen_at?}
// Re-checks open a listing again: still for sale -> last_active; "Sold" -> a real sale between
// last_active and then. Every scan turns that history into rows.

const H = 3600000;
const RECHECK_MIN_AGE = 3 * H;    // first re-check once a listing has been seen this long
const RECHECK_EVERY = 6 * H;      // then at most this often
const TRACK_FOR = 30 * DAY_MS;    // stop following a listing after this
const SOLD_ROWS_FOR = 90 * DAY_MS;  // sales found within this window feed the model
const RECHECK_MAX = 6;            // re-checks folded into one scan

/** The item id in a Vinted URL. */
export const iidOf = (url) => url.replace(/\/+$/, '').split('/').at(-1).split('-')[0].split('?')[0];

function due(seen, now, needRelevant) {
  const out = [];
  for (const [iid, rec] of seen) {
    if (rec.resolved || (needRelevant && !rec.relevant)) continue;
    const first = parseTime(rec.first_seen);
    if (now - first < RECHECK_MIN_AGE || now - first > TRACK_FOR) continue;
    const last = rec.last_checked;
    if (last && now - parseTime(last) < RECHECK_EVERY) continue;
    out.push([last || '', rec.first_seen, iid]);
  }
  return out.sort(compareTuples);  // never checked first, then the longest since a check
}

function compareTuples(a, b) {
  for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] < b[i] ? -1 : 1;
  return 0;
}

const loadSeen = async (store, query) => (await store.loadCache(slug(query), 'vinted'))?.seen ?? new Map();

/** Vinted listings from earlier scans of this query worth re-checking during this scan. */
export async function recheckIds(store, query, now = Date.now()) {
  const seen = await loadSeen(store, query);
  const d = due(seen, now, true);
  return (d.length ? d : due(seen, now, false)).slice(0, RECHECK_MAX).map((x) => x[2]);
}

/** Across every scanned item: listings the extension should re-check in the background. Only ones
 *  that matched the item in their scan ("relevant"). */
export async function dueRechecks(store, limit = 20, now = Date.now()) {
  const all = [];
  for (const { query, seen } of await store.vintedHistories()) {
    for (const [last, first, iid] of due(seen, now, true)) all.push([last, first, query, iid]);
  }
  return all.sort(compareTuples).slice(0, limit).map(([, , q, iid]) => ({ id: iid, query: q }));
}

/** Update one listing's history from a re-check page; returns active / sold / deleted / unknown. */
export function applyRecheck(rec, page) {
  const info = parseItemPage(page.html, page.status ?? 200);
  const when = isoformat(pageTime(page));
  rec.last_checked = when;
  if (info.status === 'sold') Object.assign(rec, { resolved: 'sold', sold_seen_at: when });
  else if (info.status === 'active') rec.last_active = when;
  else if (info.status === 'deleted') rec.resolved = 'deleted';
  return info.status;
}

/** Rows from re-check history: a sale between the last time it was seen for sale and the check that
 *  found it sold; or survival up to the last time it was still for sale. Entry age = when first seen. */
export function historyRows(seen, cal, now, ids = null, skipActive = new Set()) {
  const rows = [];
  for (const [iid, rec] of seen) {
    if (ids !== null && !ids.has(iid)) continue;
    const b = cal.bounds(iid);
    if (b === null || !rec.card) continue;
    const age = (t) => Math.max((parseTime(t) / 1000 - b.mid) / 86400, 0);
    const entry = age(rec.first_seen);
    const a = Math.max(age(rec.last_active || rec.first_seen), entry);
    const base = { ...vintedRow(rec.card, now), start_source: 'id_calibration', listed_at: isoFromTs(b.mid) };
    if (rec.resolved === 'sold' && now - parseTime(rec.sold_seen_at) <= SOLD_ROWS_FOR) {
      const hi = Math.max(age(rec.sold_seen_at), a + 1e-3);
      rows.push({ ...base, obs_type: 'sold_interval', t_days: round4(a), t_upper_days: round4(hi), t_entry_days: round4(entry),
        sold_at: rec.sold_seen_at });
    } else if (!rec.resolved && rec.last_active && !skipActive.has(iid) && a > entry) {
      rows.push({ ...base, obs_type: 'active', t_days: round4(a), t_upper_days: null, t_entry_days: round4(entry) });
    }
  }
  return rows;
}

const round4 = (x) => pyRound(x, 4);

export async function vintedRows(pages, query, { store }) {
  const cal = new VintedCalibration(await store.vintedCalibration());
  const now = latest(pages);
  const seen = await loadSeen(store, query);

  let cards = [];
  for (const p of pages) if (p.kind === 'catalog') cards.push(...parseCatalog(p.html));
  cards = dedupe(cards);
  if (cards.length) cal.addExact(Math.max(...cards.map((c) => Number(c.item_id))), now, 60);
  for (const p of pages) {
    if (p.kind === 'item') {
      const info = parseItemPage(p.html, p.status ?? 200);
      if (info.age_hours) cal.addAge(iidOf(p.url), info.age_hours, pageTime(p));
    }
  }
  await store.saveVintedCalibration(cal);

  const outcome = { active: 0, sold: 0, deleted: 0, unknown: 0 };
  for (const p of pages) {
    if (p.kind === 'recheck' && seen.has(iidOf(p.url))) outcome[applyRecheck(seen.get(iidOf(p.url)), p)]++;
  }
  const today = new Set(cards.map((c) => c.item_id));
  const soldIds = new Set([...seen].filter(([, v]) => v.resolved === 'sold').map(([k]) => k));
  const hist = historyRows(seen, cal, now, null, today);
  // today's catalogue: what's still listed (stock sampling gives speed)
  const fresh = vintedActiveRows(cards, cal, now).filter((r) => !soldIds.has(r.listing_id));
  const rows = hist.concat(fresh);
  for (const c of cards) if (!seen.has(c.item_id)) seen.set(c.item_id, { first_seen: isoformat(now), card: c });
  const stats = { catalog: cards.length, fresh_rows: fresh.length, rechecked_sold: outcome.sold,
    rechecked_active: outcome.active, history_sales: hist.filter((r) => r.obs_type === 'sold_interval').length,
    calibration_samples: cal.samples.size };
  await saveCache(store, query, 'vinted', rows, { stats, source: 'extension', seen });
  return [rows, stats];
}

/** After a scan: remember which Vinted listings matched the item, so background re-checks follow those. */
export async function markRelevant(store, query, ids) {
  if (ids.length) await store.markRelevant(slug(query), ids.map(String));
}

/** results: [{query, page}] from the extension's background run. Updates each search's history and its
 *  saved rows, so "Use saved data" and the next scan both see the sales. */
export async function applyBackgroundRechecks(results, { store }) {
  const cal = new VintedCalibration(await store.vintedCalibration());
  const byQ = new Map();
  for (const r of results) {
    if (!byQ.has(r.query)) byQ.set(r.query, []);
    byQ.get(r.query).push(r.page);
  }
  const out = { active: 0, sold: 0, deleted: 0, unknown: 0, skipped: 0, session_refresh: 0 };
  for (const [q, all] of byQ) {
    // placeholders say nothing about the listing: leave it due for the next run
    const pages = all.filter((pg) => !isSessionRefresh(pg));
    out.session_refresh += all.length - pages.length;
    if (!pages.length) continue;
    const data = await store.loadCache(slug(q), 'vinted');
    if (!data) { out.skipped += pages.length; continue; }
    const seen = data.seen ?? new Map();
    const touched = new Set();
    for (const page of pages) {
      const iid = iidOf(page.url);
      if (!seen.has(iid)) { out.skipped++; continue; }
      out[applyRecheck(seen.get(iid), page)]++;
      touched.add(iid);
    }
    const now = latest(pages);
    data.rows = (data.rows || []).filter((r) => !touched.has(r.listing_id)).concat(historyRows(seen, cal, now, touched));
    data.seen = seen;
    await store.saveCache(slug(q), 'vinted', data);
  }
  return out;
}

/** Pages -> [rows, stats]. queries: {ebay, vinted}. */
export async function ingest(pages, queries, ctx) {
  const vinted = pages.filter((p) => p.platform === 'vinted');
  const by = { ebay: pages.filter((p) => p.platform === 'ebay'), vinted: vinted.filter((p) => !isSessionRefresh(p)) };
  let rows = [];
  const stats = {};
  if (by.vinted.length < vinted.length) stats.vinted_session_refresh = vinted.length - by.vinted.length;
  if (queries.ebay && (by.ebay.length || ebayApiConfigured(ctx.env || {}))) {
    const [r, s] = await ebayRows(by.ebay, queries.ebay, ctx);
    rows = rows.concat(r);
    stats.ebay = s;
  }
  if (by.vinted.length && queries.vinted) {
    const [r, s] = await vintedRows(by.vinted, queries.vinted, ctx);
    rows = rows.concat(r);
    stats.vinted = s;
  }
  return [rows, stats];
}
