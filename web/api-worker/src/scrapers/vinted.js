// Vinted UK: catalogue and item pages (backend/scrapers/vinted_parse.py), item-ID -> listing time
// calibration (vinted_calibration.py) and cards -> ListingRows (vinted_client.py). Port of those.
//
// Catalogue pages: listings in [data-testid="grid-item"]; each link's title attribute carries
//   "Nike air force 1 size 7, Brand: Nike, Condition: Good, Size: 7, 18.00 £, 19.60 £"
// (the second price includes Vinted's buyer fee). Item pages: JSON-LD offers.availability is
// "InStock" for live items; sold ones keep their page with a "Sold" mark; deleted ones 404. Upload age
// is relative text in [itemprop="upload_date"]. An expired session sends every page to
// /session-refresh, a placeholder with no listings.

import { median, pyRound } from '../engine/numeric.js';
import { DAY_MS, isoFromTs, isoformat } from '../time.js';
import { attr, getText, parse } from './html.js';

const RE_SUMMARY = /^(?<title>.*), Brand: (?<brand>.*?), Condition: (?<cond>[^,]+?)(?:, Size: (?<size>.*?))?, (?<price>[\d,]+\.\d{2}) £, (?<total>[\d,]+\.\d{2}) £$/;
const RE_ITEM_ID = /\/items\/(\d{8,12})/;
const RE_AGE = /(?<n>\d+|an?|a few)\s*(?<unit>sec|second|min|minute|hour|day|week|month|year)s?\s+ago/i;
const UNIT_HOURS = { sec: 1 / 3600, second: 1 / 3600, min: 1 / 60, minute: 1 / 60, hour: 1.0, day: 24.0, week: 168.0,
  month: 720.0, year: 8760.0 };
const RE_SESSION_REFRESH = /\/session-refresh(\?|$)/i;
const RE_TITLE = /<title[^>]*>\s*([^<]*?)\s*<\/title>/i;
export const CONDITION_TIER = { 'New with tags': 'mint', 'New without tags': 'mint', 'Very good': 'very_good', Good: 'good',
  Satisfactory: 'fair' };
const SHIPPING_GBP = 3.20;  // typical UK postage shown at checkout

/** A collected page that is Vinted's session-refresh placeholder, not the page asked for. */
export function isSessionRefresh(page) {
  if (RE_SESSION_REFRESH.test(page.final_url || '')) return true;
  const m = (page.html || '').slice(0, 20000).match(RE_TITLE);
  return Boolean(m) && m[1].toLowerCase() === 'session refresh';
}

const num = (s) => Number(s.replaceAll(',', ''));

export function parseCatalog(html) {
  const doc = parse(html);
  const out = [], seen = new Set();
  for (const g of doc.querySelectorAll('[data-testid="grid-item"]')) {
    const a = g.querySelector('a[href*="/items/"]');
    if (!a) continue;
    const mId = (a.getAttribute('href') || '').match(RE_ITEM_ID);
    const m = (a.getAttribute('title') || '').trim().match(RE_SUMMARY);
    if (!mId || !m || seen.has(mId[1])) continue;
    seen.add(mId[1]);
    const fav = g.querySelector('[data-testid="favourite-count-text"]');
    const favTxt = fav ? getText(fav, '', true) : '';
    const price = num(m.groups.price), total = num(m.groups.total);
    out.push({
      item_id: mId[1], title: m.groups.title, brand: m.groups.brand, condition_raw: m.groups.cond, size: m.groups.size ?? null,
      price_gbp: price, buyer_fee_gbp: pyRound(total - price, 2), favourite_count: /^\d+$/.test(favTxt) ? Number(favTxt) : 0,
      url: `https://www.vinted.co.uk/items/${mId[1]}`,
    });
  }
  return out;
}

/** '3 weeks ago' -> [17.5*24, 28*24] hours. Bounds cover both floor and round display conventions. */
export function ageIntervalHours(text) {
  if (!text) return null;
  const m = text.match(RE_AGE);
  if (!m) return /just now|seconds? ago/i.test(text) ? [0.0, 1 / 60] : null;
  const nRaw = m.groups.n.toLowerCase();
  const n = ['a', 'an'].includes(nRaw) ? 1 : nRaw === 'a few' ? 3 : Number(nRaw);
  const u = UNIT_HOURS[m.groups.unit.toLowerCase()];
  return [Math.max(n - 0.5, 0) * u, (n + 1) * u];
}

/** Status of one item page: 'active', 'sold', 'deleted' or 'unknown' (hidden / reserved). */
export function parseItemPage(html, httpStatus) {
  if (httpStatus === 404 || html == null) return { status: 'deleted', age_hours: null };
  const doc = parse(html);
  let avail = null;
  const ld = doc.querySelector('script[type="application/ld+json"]');
  if (ld && ld.textContent) {
    try {
      const j = JSON.parse(ld.textContent);
      if (j && typeof j === 'object' && !Array.isArray(j)) avail = (j.offers && typeof j.offers === 'object' ? j.offers.availability : null) ?? null;
    } catch { /* not JSON */ }
  }
  // Vinted writes itemProp="upload_date": BeautifulSoup lowercases attribute names, but linkedom's
  // selectors and getAttribute are case-sensitive, so check the attribute ignoring case
  const up = [...doc.querySelectorAll('*')].find((el) => attr(el, 'itemprop') === 'upload_date') ?? null;
  const age = ageIntervalHours(up ? getText(up, '', true) : null);
  const hasBuy = doc.querySelector('[data-testid="item-buy-button"]') !== null;
  let status;
  if (avail === 'InStock' && hasBuy) status = 'active';
  else if (avail === null && /\bSold\b/.test(getText(doc.body || doc, ' '))) status = 'sold';
  else status = 'unknown';
  return { status, age_hours: age };
}

// ------------------------------------------------------------------ item ID -> listing time
// Item IDs are one global counter whose rate varies (slower at night and weekends), so we keep samples
// (id, earliest start, latest start) and bracket any other ID between its neighbours.

export class VintedCalibration {
  /** samples: Map(id -> [lo, hi]) in unix seconds. */
  constructor(samples = new Map()) {
    this.samples = samples;
    this.changed = new Set();
    this.env = null;
  }

  set(id, lo, hi) {
    this.samples.set(Number(id), [lo, hi]);
    this.changed.add(Number(id));
    this.env = null;
  }

  /** An item page said it was uploaded between ageHours[0] and ageHours[1] before observedAt (ms). */
  addAge(itemId, [h0, h1], observedAt) {
    this.set(itemId, (observedAt - h1 * 3600000) / 1000, (observedAt - h0 * 3600000) / 1000);
  }

  /** E.g. the top of a newest-first page, seen within minutes of being listed. */
  addExact(itemId, when, slackMinutes = 10) {
    this.set(itemId, when / 1000 - slackMinutes * 60, when / 1000);
  }

  /** Sample IDs that agree with a counter that only goes up. A bumped listing shows its bump time as
   *  "Uploaded", so an old ID can claim a very recent upload; two samples conflict when the older ID's
   *  earliest start is after the newer ID's latest start. Greedily drop the sample in the most
   *  conflicts until none remain (ties: the lowest ID; Python's order there is its set order). */
  consistentIds() {
    const alive = [...this.samples.keys()].sort((a, b) => a - b);
    for (;;) {
      const counts = new Array(alive.length).fill(0);
      for (let i = 0; i < alive.length; i++) {
        for (let j = i + 1; j < alive.length; j++) {
          if (this.samples.get(alive[i])[0] > this.samples.get(alive[j])[1]) { counts[i]++; counts[j]++; }
        }
      }
      let worst = -1;
      counts.forEach((c, i) => { if (c > 0 && (worst < 0 || c > counts[worst])) worst = i; });
      if (worst < 0) return alive;
      alive.splice(worst, 1);
    }
  }

  /** Monotone counter => start(n) >= every older sample's earliest bound (running max) and <= every
   *  newer sample's latest bound (running min from the other end). */
  envelope() {
    if (this.env) return this.env;
    const ids = this.consistentIds();
    const loEnv = [], hiEnv = new Array(ids.length);
    let run = -Infinity;
    for (const i of ids) { run = Math.max(run, this.samples.get(i)[0]); loEnv.push(run); }
    run = Infinity;
    for (let k = ids.length - 1; k >= 0; k--) { run = Math.min(run, this.samples.get(ids[k])[1]); hiEnv[k] = run; }
    this.env = { ids, loEnv, hiEnv };
    return this.env;
  }

  /** {lo, hi, mid} in unix seconds, or null outside the calibration. */
  bounds(itemId) {
    const { ids, loEnv, hiEnv } = this.envelope();
    const n = Number(itemId);
    if (ids.length < 2 || n < ids[0] || n > ids.at(-1)) return null;
    let j = 0, top = ids.length;
    while (j < top) { const m = (j + top) >> 1; if (ids[m] < n) j = m + 1; else top = m; }  // bisect_left
    let lo, hi;
    if (ids[j] === n) [lo, hi] = [loEnv[j], hiEnv[j]];
    else [lo, hi] = [loEnv[j - 1], hiEnv[j]];  // older side bounds from below, newer side from above
    if (hi < lo) [lo, hi] = [hi, lo];  // samples disagree (display rounding); widen to cover both
    // point estimate: interpolate between the neighbouring samples' centres
    let est;
    const centre = (id) => (this.samples.get(id)[0] + this.samples.get(id)[1]) / 2;
    if (ids[j] === n) est = centre(n);
    else {
      const a = ids[j - 1], b = ids[j];
      est = centre(a) + (n - a) / (b - a) * (centre(b) - centre(a));
    }
    return { lo, hi, mid: Math.min(Math.max(est, lo), hi) };
  }
}

// ------------------------------------------------------------------ cards -> rows

export function vintedRow(c, now) {
  return {
    listing_id: c.item_id, platform: 'vinted', title: c.title, url: c.url,
    price_gbp: c.price_gbp, shipping_gbp: SHIPPING_GBP, buyer_fee_gbp: c.buyer_fee_gbp,
    total_buyer_price_gbp: pyRound(c.price_gbp + SHIPPING_GBP + c.buyer_fee_gbp, 2),
    condition_tier: CONDITION_TIER[c.condition_raw] ?? null, condition_raw: c.condition_raw,
    observed_at: isoformat(now), favourite_count: c.favourite_count,
    best_offer_accepted: null, is_outlier: false, outlier_reason: null, sold_at: null,
  };
}

const STOCK_MIN_ROWS = 20;
const STOCK_MAX_DAYS = 120.0;

export function activeRows(cards, cal, now) {
  const rows = [], mids = new Map();
  for (const c of cards) {
    const b = cal.bounds(c.item_id);
    if (b === null) continue;
    // censored at the youngest plausible age; a single scrape only shows survivors: entry age = age now
    const ageMin = Math.max((now / 1000 - b.hi) / 86400, 0);
    rows.push({ ...vintedRow(c, now), obs_type: 'active', t_days: pyRound(ageMin, 4), t_upper_days: null,
      t_entry_days: pyRound(ageMin, 4), start_source: 'id_calibration', listed_at: isoFromTs(b.mid) });
    mids.set(c.item_id, b.mid);
  }
  // Newest-first catalogue read back to age A: the age spread of what's still listed tells how fast
  // things go (stock sampling). Ages use the best-guess start, not the youngest bound.
  if (rows.length >= STOCK_MIN_ROWS) {
    const ages = new Map(rows.map((r) => [r.listing_id, Math.max((now / 1000 - mids.get(r.listing_id)) / 86400, 0)]));
    // A = how far back the pages reach: the typical age of the last 5% of cards in catalogue order
    const inOrder = rows.map((r) => ages.get(r.listing_id));
    let window = median(inOrder.slice(-Math.max(3, Math.floor(inOrder.length / 20))));
    window = Math.min(window, STOCK_MAX_DAYS);  // past this, listings say more about sellers than speed
    if (window >= 1.0) {
      for (const r of rows) {
        const a = pyRound(ages.get(r.listing_id), 4);
        Object.assign(r, { t_days: a, t_entry_days: a, stock_window_days: pyRound(window, 4) });
      }
    }
  }
  return rows;
}

export { DAY_MS };
