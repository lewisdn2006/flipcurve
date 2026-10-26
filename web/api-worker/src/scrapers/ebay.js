// eBay UK: search pages -> cards (backend/scrapers/ebay_parse.py), item-number calibration
// (ebay_calibration.py), cards -> ListingRows (ebay_client.py) and the official Browse API
// (ebay_api.py). Port of those files; checked against the Python's results in test/node.
//
// Card text as of 24 Oct 2026, read with regexes so most markup changes don't matter:
//   newest-first active search:  "... | £53.83 | or Best Offer | +£28.00 delivery | 24-Oct 21:48"
//   sold search:                 "Sold 24 Oct 2026 | ... | £40.38 | Best Offer accepted | +£33.66 delivery"

import { pyRound } from '../engine/numeric.js';
import { DAY_MS, UK, isoFromTs, isoformat, parseTime, zonedParts, zonedToUtc } from '../time.js';
import { getText, parse, strippedStrings } from './html.js';

const MONTHS = Object.fromEntries('Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec'.split(' ').map((m, i) => [m, i + 1]));
const MON = 'Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec';
const RE_START = new RegExp(`\\b(\\d{1,2})-(${MON})(?:-(\\d{2}))? (\\d{2}):(\\d{2})\\b`);
const RE_SOLD = new RegExp(`\\bSold\\s+(\\d{1,2}) (${MON}) (\\d{4})\\b`);
const RE_PRICE = /£\s?([\d,]+\.\d{2})/;
const RE_DELIVERY = /\+£\s?([\d,]+\.\d{2}) (?:delivery|postage)/;
const RE_BIDS = /\b(\d+) bids?\b/;
const CONDITIONS = [  // longest first so "Pre-owned - Excellent" wins over "Pre-owned"
  'Pre-owned - Excellent', 'Pre-owned - Good', 'Pre-owned - Fair', 'New with tags', 'New without tags',
  'New with box', 'New (other)', 'Opened – never used', 'Brand new', 'Pre-owned', 'Used',
  'For parts or not working', 'Parts only'];

const num = (s) => Number(s.replaceAll(',', ''));

/** '24-Oct 21:48' is UK local time with no year: the most recent such moment <= now (ms). */
function startTime(m, now) {
  const [, day, mon, yy, hh, mm] = m;
  let year = yy ? 2000 + Number(yy) : zonedParts(now, UK).y;
  let t = zonedToUtc(year, MONTHS[mon], Number(day), Number(hh), Number(mm), 0, UK);
  if (!yy && t > now) t = zonedToUtc(year - 1, MONTHS[mon], Number(day), Number(hh), Number(mm), 0, UK);
  return t;
}

/** Search-result HTML -> cards. now: ms (when the page was loaded). */
export function parseCards(html, now) {
  const doc = parse(html);
  const cards = [];
  for (const li of doc.querySelectorAll('li[data-listingid]')) {
    const lid = li.getAttribute('data-listingid') || '';
    if (!/^\d{12}$/.test(lid)) continue;  // sponsored placeholders carry other ids
    const titleEl = li.querySelector('.s-card__title'), priceEl = li.querySelector('.s-card__price');
    if (!titleEl || !priceEl) continue;
    const title = getText(titleEl, ' ', true).replace('Opens in a new window or tab', '').trim();
    const priceTxt = getText(priceEl, ' ', true);
    if (priceTxt.includes(' to ')) continue;  // variation listings show a price range
    const pm = priceTxt.match(RE_PRICE);
    if (!pm) continue;
    const text = strippedStrings(li).join(' | ');
    const start = text.match(RE_START), sold = text.match(RE_SOLD), ship = text.match(RE_DELIVERY);
    cards.push({
      item_id: lid, title, price_gbp: num(pm[1]),
      shipping_gbp: ship ? num(ship[1]) : (text.includes('Free delivery') || text.includes('Free postage') ? 0.0 : null),
      condition_raw: CONDITIONS.find((c) => text.includes(c)) ?? null,
      start_time: start ? startTime(start, now) : null, start_zone: UK,
      sold_date: sold ? { y: Number(sold[3]), m: MONTHS[sold[2]], d: Number(sold[1]) } : null,
      best_offer_accepted: text.includes('Best Offer accepted'),
      is_auction: RE_BIDS.test(text),
      from_abroad: /\bfrom (?!United Kingdom)[A-Z][a-z]+/.test(text),
      url: `https://www.ebay.co.uk/itm/${lid}`,
    });
  }
  return cards;
}

/** eBay's condition text -> our 4 tiers (null = unknown; the engine treats it as 'good'). */
export function conditionTier(raw) {
  if (raw == null) return null;
  if (['Brand new', 'New', 'New with tags', 'New with box', 'New without tags'].includes(raw)) return 'mint';
  if (['New (other)', 'Opened – never used', 'Pre-owned - Excellent'].includes(raw)) return 'very_good';
  if (['Pre-owned - Good', 'Pre-owned', 'Used', 'Good'].includes(raw)) return 'good';
  if (raw === 'Pre-owned - Fair') return 'fair';
  return null;  // parts / not working: the outlier filter drops these unless the item is broken
}

// ------------------------------------------------------------------ item-number calibration
// eBay item numbers come in ~100 parallel blocks (first three digits); within a block they rise with
// time. Newest-first searches show start times, so every active listing seen is a calibration point;
// any other number (a SOLD listing) is bracketed between its two neighbours in the same block.

const BRACKET_SLACK_DAYS = 0.1;  // ~2-4% of points are out of order (relists etc.)
export const blockOf = (id) => String(id).slice(0, 3);

/** Points come from a store (D1 or memory): block(b) -> {ids, ts}, add([[id, ts]], source). */
export class EbayCalibration {
  constructor(store) {
    this.store = store;
    this.cache = new Map();
  }

  async add(points, source = 'search') {
    if (!points.length) return 0;
    const n = await this.store.ebayAdd(points.map(([id, ms]) => [Number(id), blockOf(id), ms / 1000]), source);
    for (const [id] of points) this.cache.delete(blockOf(id));
    return n;
  }

  async block(b) {
    if (!this.cache.has(b)) this.cache.set(b, await this.store.ebayBlock(b));
    return this.cache.get(b);
  }

  /** Bracket an item's start between its neighbours: {lo, hi, mid} in seconds, or null if outside. */
  async estimateStart(itemId, minPoints = 5) {
    const { ids, ts } = await this.block(blockOf(itemId));
    const n = Number(itemId);
    if (ids.length < minPoints || n < ids[0] || n > ids.at(-1)) return null;
    let j = 0, hi = ids.length;
    while (j < hi) { const m = (j + hi) >> 1; if (ids[m] < n) j = m + 1; else hi = m; }  // bisect_left
    if (ids[j] === n) return { lo: ts[j], hi: ts[j], mid: ts[j] };  // we saw this listing ourselves
    const [aId, bId, aT, bT] = [ids[j - 1], ids[j], ts[j - 1], ts[j]];
    const mid = aT + (bT - aT) * (n - aId) / (bId - aId);
    const slack = BRACKET_SLACK_DAYS * 86400;
    return { lo: Math.min(aT, bT) - slack, hi: Math.max(aT, bT) + slack, mid };
  }
}

// ------------------------------------------------------------------ cards -> rows

function baseRow(c, now) {
  const ship = c.shipping_gbp || 0.0;
  const fee = 0.0;  // eBay's buyer fee isn't added here (platform_config: buyer_fee_pct 0)
  return {
    listing_id: c.item_id, platform: 'ebay', title: c.title, url: c.url,
    price_gbp: c.price_gbp, shipping_gbp: ship, buyer_fee_gbp: fee,
    total_buyer_price_gbp: pyRound(c.price_gbp + ship + fee, 2),
    condition_tier: conditionTier(c.condition_raw), condition_raw: c.condition_raw,
    observed_at: isoformat(now), favourite_count: null,
    best_offer_accepted: c.best_offer_accepted || null,
    is_outlier: false, outlier_reason: null,
  };
}

export function activeRows(cards, now) {
  const rows = [];
  for (const c of cards) {
    if (c.start_time == null || c.from_abroad) continue;
    const age = (now - c.start_time) / DAY_MS;
    rows.push({ ...baseRow(c, now), obs_type: 'active', t_days: pyRound(Math.max(age, 0), 4), t_upper_days: null,
      start_source: 'exact', listed_at: isoformat(c.start_time, c.start_zone || 'UTC', c.start_micro), sold_at: null });
  }
  return rows;
}

const ukMidnight = ({ y, m, d }, plusDays = 0) => zonedToUtc(y, m, d + plusDays, 0, 0, 0, UK);

export async function soldRows(cards, cal, now) {
  const rows = [];
  for (const c of cards) {
    if (c.sold_date == null || c.from_abroad) continue;
    const dayStart = ukMidnight(c.sold_date), dayEnd = ukMidnight(c.sold_date, 1);
    const est = await cal.estimateStart(c.item_id);
    const base = { ...baseRow(c, now), sold_at: isoformat(dayStart, UK) };
    if (est !== null) {
      const lo = (dayStart / 1000 - est.hi) / 86400;
      const hi = (dayEnd / 1000 - est.lo) / 86400;
      if (hi >= 0) {
        rows.push({ ...base, obs_type: 'sold_interval', t_days: pyRound(Math.max(lo, 0), 4), t_upper_days: pyRound(hi, 4),
          start_source: 'id_calibration', listed_at: isoFromTs(est.mid) });
        continue;
      }
    }
    rows.push({ ...base, obs_type: 'sold_unknown_time', t_days: null, t_upper_days: null, start_source: 'unknown', listed_at: null });
  }
  return rows;
}

/** Parsed cards -> a cohort of ListingRows (maths doc, section 5): only listings that started after the
 *  earliest time both searches fully cover. *Complete: the pages reached the end of the results. */
export async function buildRows(actCards, soldCards, cal, now, actComplete = false, soldComplete = false, soldRefused = null) {
  let windowStart = now - 90 * DAY_MS;  // eBay's sold search only goes back ~90 days
  const starts = actCards.map((c) => c.start_time).filter((t) => t != null);
  if (!actComplete && starts.length) windowStart = Math.max(windowStart, Math.min(...starts));
  const soldDays = soldCards.map((c) => c.sold_date).filter(Boolean);
  if (!soldComplete && soldDays.length) {
    const d = soldDays.reduce((a, b) => (Date.UTC(b.y, b.m - 1, b.d) < Date.UTC(a.y, a.m - 1, a.d) ? b : a));
    // a listing that started after the window opened and sold must have sold after it too
    windowStart = Math.max(windowStart, ukMidnight(d, 1));
  }
  if (soldRefused && !soldCards.length) {
    // no sold side: active listings are survivors we happened to see: prices, but no speed information
    const a = activeRows(actCards, now);
    for (const r of a) r.t_entry_days = r.t_days;
    return [a, { active: a.length, sold: 0, sold_dated: 0, coverage: null, window_days: null, sold_refused: soldRefused }];
  }
  const a = activeRows(actCards, now).filter((r) => parseTime(r.listed_at) >= windowStart);
  const sAll = await soldRows(soldCards, cal, now);
  const s = sAll.filter((r) => r.obs_type !== 'sold_interval' || parseTime(r.listed_at) >= windowStart);
  for (const r of [...a, ...s]) r.t_entry_days = r.obs_type !== 'sold_unknown_time' ? 0.0 : null;
  const dated = s.filter((r) => r.obs_type === 'sold_interval');
  return [a.concat(s), {
    active: a.length, sold: s.length, sold_dated: dated.length,
    coverage: sAll.length ? pyRound(sAll.filter((r) => r.obs_type === 'sold_interval').length / sAll.length, 3) : null,
    window_days: pyRound((now - windowStart) / DAY_MS, 2),
  }];
}

// ------------------------------------------------------------------ official Browse API (active listings)

const TOKEN_URL = 'https://api.ebay.com/identity/v1/oauth2/token';
const SEARCH_URL = 'https://api.ebay.com/buy/browse/v1/item_summary/search';
const SCOPE = 'https://api.ebay.com/oauth/api_scope';
let cachedToken = null;  // per Worker instance: {key, token, expires}

export const ebayApiConfigured = (env) => Boolean(env.EBAY_CLIENT_ID && env.EBAY_CLIENT_SECRET);

export class BrowseAPI {
  constructor(env, fetchFn = fetch) {
    this.id = env.EBAY_CLIENT_ID;
    this.secret = env.EBAY_CLIENT_SECRET;
    this.fetch = (url, init) => fetchFn(url, init);  // fetch must be called unbound in Workers ("Illegal invocation")
  }

  async token() {
    if (cachedToken && cachedToken.key === this.id && Date.now() < cachedToken.expires - 60000) return cachedToken.token;
    const r = await this.fetch(TOKEN_URL, { method: 'POST', signal: AbortSignal.timeout(20000),
      headers: { Authorization: `Basic ${btoa(`${this.id}:${this.secret}`)}`, 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ grant_type: 'client_credentials', scope: SCOPE }) });
    if (!r.ok) throw new Error(`eBay token: HTTP ${r.status}`);
    const j = await r.json();
    cachedToken = { key: this.id, token: j.access_token, expires: Date.now() + Number(j.expires_in || 7200) * 1000 };
    return cachedToken.token;
  }

  async search(q, { limit = 200, offset = 0, sort = 'newlyListed', ukOnly = true } = {}) {
    const params = new URLSearchParams({ q, limit: String(limit), offset: String(offset), sort });
    if (ukOnly) params.set('filter', 'itemLocationCountry:GB');  // like the website search's "UK only"
    const r = await this.fetch(`${SEARCH_URL}?${params}`, { signal: AbortSignal.timeout(20000),
      headers: { Authorization: `Bearer ${await this.token()}`, 'X-EBAY-C-MARKETPLACE-ID': 'EBAY_GB' } });
    if (!r.ok) throw new Error(`eBay search: HTTP ${r.status}`);
    return r.json();
  }

  async activeCards(q, pages = 5) {
    const out = [];
    for (let p = 0; p < pages; p++) {
      const items = (await this.search(q, { offset: p * 200 })).itemSummaries || [];
      out.push(...items.map(toCard).filter(Boolean));
      if (items.length < 200) break;
    }
    return out;
  }
}

/** One Browse API result -> a card shaped like parseCards' (times in ms, UTC). */
export function toCard(it) {
  const itemId = it.itemId || '';
  const lid = it.legacyItemId || (itemId.includes('|') ? itemId.split('|')[1] : null);
  const price = it.price || {};
  if (!lid || price.currency !== 'GBP') return null;
  let ship = null;
  for (const opt of it.shippingOptions || []) {
    const cost = (opt.shippingCost || {}).value;
    if (cost != null) { ship = Number(cost); break; }
  }
  const created = parseTime(it.itemCreationDate), origin = parseTime(it.itemOriginDate);
  const country = (it.itemLocation || {}).country;
  return {
    item_id: String(lid), title: it.title || '', price_gbp: Number(price.value), shipping_gbp: ship,
    condition_raw: it.condition ?? null,
    start_time: origin ?? created, start_zone: 'UTC',  // first listed: the right start for time-to-sell
    created_time: created,                            // this item number's own start: right for calibration
    sold_date: null, best_offer_accepted: false, is_auction: (it.buyingOptions || []).includes('AUCTION'),
    from_abroad: Boolean(country && country !== 'GB'), url: it.itemWebUrl || `https://www.ebay.co.uk/itm/${lid}`,
  };
}
