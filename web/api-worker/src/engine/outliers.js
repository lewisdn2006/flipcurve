// Junk and outlier removal before fitting. Port of backend/engine/outlier_filter.py.
//
// Rows are never deleted: each gets is_outlier / outlier_reason, so the evidence drawer can show what
// was excluded and why. Three passes:
//   1. Keywords: "box only", "spares", bundles, fakes... (repair words allowed if the item is broken)
//   2. Relevance: the title must share at least 70% of the search query's meaningful words. A
//      different version (Mid for Low, Pro / Max / Mini, kids' sizes) is tagged `related` and used
//      for selling speed with its price rescaled, never for the price level.
//   3. Price: robust (median/MAD) fences on condition-adjusted log price, per platform

import { median } from './numeric.js';

const NEGATIVE = ['box only', 'empty box', 'case only', 'bundle', 'joblot', 'job lot', 'lot of', 'replica',
  'fake', 'not genuine', 'unbranded', 'inspired', 'laces only', 'insoles only', 'x2 pairs', 'wanted', 'swap'];
const REPAIR = ['broken', 'faulty', 'spares', 'repair', 'for parts', 'not working', 'parts only'];
const STOP = new Set(['the', 'a', 'an', 'and', 'or', 'for', 'with', 'of', 'in', 'size', 'uk', 'eu', 'us', 'mens',
  "men's", 'womens', "women's", 'new', 'used']);
// Mutually exclusive versions: if the search names one, a title naming another is a different item
const EXCLUSIVE = [new Set(['low', 'mid', 'high', 'hi'])];
// Model-tier words: a title with one the search doesn't have is a different (usually pricier) item
const TIER_WORDS = new Set(['pro', 'max', 'plus', 'mini', 'ultra', 'lite', 'se', 'og', 'sp']);
const KIDS = new Set(['gs', 'junior', "junior's", 'kids', "kid's", 'infant', 'infants', 'toddler', 'baby', 'youth',
  'boys', 'girls', 'td', 'ps', 'child', 'childs', 'children', "children's", 'jnr', 'bgs']);
// Colour words: a title naming a colour the item doesn't have is another colourway
const COLOURS = new Set(['black', 'white', 'red', 'blue', 'royal', 'navy', 'green', 'grey', 'gray', 'pink', 'purple',
  'lilac', 'orange', 'yellow', 'brown', 'beige', 'tan', 'cream', 'sail', 'ivory', 'gold', 'silver', 'bronze', 'teal',
  'turquoise', 'olive', 'khaki', 'burgundy', 'maroon', 'violet', 'multi', 'multicolour', 'multicolor', 'panda', 'bred',
  'chicago', 'mocha', 'smoke', 'volt', 'lime', 'coral', 'peach', 'camo', 'rose', 'aqua', 'cyan', 'magenta', 'neon']);
// words that are versions (handled as `related`), so must_not_have doesn't throw those away
const VERSION_WORDS = new Set([...EXCLUSIVE[0], ...TIER_WORDS, ...KIDS]);
const RELEVANCE = 0.7;            // share of the query's words a title needs
const RELEVANCE_WITH_MUST = 0.5;  // ...when the identifier gave must_have words, which pin the item down
const FENCE_K = 3.0;
const SOLD_HI = 2.5, SOLD_LO = 0.3;
const TIER_RATIO = { fair: 0.65, good: 1.0, very_good: 1.32, mint: 1.9 };  // = survival DEFAULT_TIER_RATIO
const MIN_FOR_FENCES = 8;

const SYNONYMS = [[/\bt[\s-]?shirts?\b|\btees?\b/g, 'tshirt'], [/\bsneakers?\b|\btrainer\b/g, 'trainers'],
  [/\bhoodies?\b|\bhooded\b/g, 'hoodie'], [/\bjumpers?\b|\bsweaters?\b/g, 'jumper']];

const WORD = /[a-z0-9']+/g;
const norm = (text) => SYNONYMS.reduce((t, [pat, rep]) => t.replace(pat, rep), (text || '').toLowerCase());
const words = (text) => new Set((norm(text).match(WORD) || []).filter((w) => w.length > 1 && !STOP.has(w)));
const allWords = (text) => new Set((text || '').toLowerCase().match(WORD) || []);
const orderedWords = (text) => (text || '').toLowerCase().replaceAll('/', ' ').match(WORD) || [];
const inter = (a, b) => new Set([...a].filter((x) => b.has(x)));
const union = (a, b) => new Set([...a, ...b]);

function mark(r, reason) {
  if (!r.is_outlier) {
    r.is_outlier = true;
    r.outlier_reason = reason;
  }
}

const UK_SIZE = /\buk\s*-?\s*(\d{1,2}(?:\.5)?)\b|\b(\d{1,2}(?:\.5)?)\s*uk\b/;
const FOOTWEAR = new Set(['trainers', 'trainer', 'shoes', 'shoe', 'sneakers', 'sneaker', 'boots', 'boot', 'footwear',
  'jordan', 'dunk', 'yeezy', 'loafers', 'heels', 'sandals', 'slides']);

export function ukSize(text) {
  const m = (text || '').toLowerCase().match(UK_SIZE);
  return m ? Number(m[1] || m[2]) : null;
}

/** Which different version of the searched item this title is ("mid", "pro", "kids"), or null. For
 *  footwear, an adult item (UK 7+) vs a listing in UK 6 or under counts as kids' sizing. */
export function versionMismatch(title, query, itemText = '', footwear = false) {
  const t = allWords(title), q = union(allWords(query), allWords(itemText));
  for (const group of EXCLUSIVE) {
    const mine = inter(q, group);
    if (mine.size && !inter(t, mine).size) {
      const other = [...t].filter((w) => group.has(w) && !mine.has(w));
      if (other.length) return other.sort()[0];
    }
  }
  const extra = [...t].filter((w) => TIER_WORDS.has(w) && !q.has(w));
  if (extra.length) return extra.sort()[0];
  if (inter(t, KIDS).size && !inter(q, KIDS).size) return 'kids';
  if (footwear) {
    const mine = ukSize(itemText), theirs = ukSize(title);
    if (mine !== null && theirs !== null && mine >= 7 && theirs <= 6) return 'kids';
  }
  return null;
}

/** term may be several words ("iphone 13") and alternatives ("triple black|all black"). */
function has(textWords, term) {
  const padded = ` ${textWords.join(' ')} `;
  return term.split('|').some((alt) => alt.trim() && padded.includes(` ${orderedWords(alt).join(' ')} `));
}

/** Why this title is a different item (not just a different version), or null. */
export function otherItemReason(title, query, itemText = '', mustHave = [], mustNotHave = []) {
  const ws = orderedWords(title);
  for (const term of mustHave) {
    if (!has(ws, term)) return `missing '${term.split('|')[0]}'`;
  }
  for (const term of mustNotHave) {
    if (orderedWords(term).every((w) => VERSION_WORDS.has(w))) continue;  // kept as related versions instead
    if (has(ws, term)) return `different item: ${term}`;
  }
  const qAll = union(allWords(query), allWords(itemText));
  if (ws.includes('x') && !qAll.has('x')) return 'collaboration';  // "Travis Scott x Nike ..."
  const mine = inter(qAll, COLOURS);
  if (mine.size) {
    const other = [...new Set(ws)].filter((w) => COLOURS.has(w) && !mine.has(w));
    if (other.length) return `different colour: ${other.sort()[0]}`;
  }
  return null;
}

const adjLog = (r) => Math.log(r.price_gbp / (TIER_RATIO[r.condition_tier || 'good'] ?? 1.0));

/** queries: platform -> search query; itemText: the item's title/model; mustHave / mustNotHave: the
 *  identifier's title rules. Returns copies of the rows with outliers marked. */
export function filterRows(rows, queries, itemIsBroken = false, { itemText = '', mustHave = [], mustNotHave = [], footwear = null } = {}) {
  rows = rows.map((r) => ({ ...r, is_outlier: false, outlier_reason: null, related: null }));
  if (footwear === null) footwear = inter(allWords(`${itemText} ${Object.values(queries).join(' ')}`), FOOTWEAR).size > 0;
  const badWords = NEGATIVE.concat(itemIsBroken ? [] : REPAIR);
  for (const r of rows) {
    const title = r.title.toLowerCase();
    const hit = badWords.find((w) => title.includes(w));
    if (hit) { mark(r, `keyword: ${hit}`); continue; }
    const cond = (r.condition_raw || '').toLowerCase();
    if (!itemIsBroken && (cond.includes('parts') || cond.includes('not working'))) { mark(r, 'condition: for parts'); continue; }
    const query = queries[r.platform] ?? '';
    const q = words(query);
    if (q.size && inter(q, words(r.title)).size < (mustHave.length ? RELEVANCE_WITH_MUST : RELEVANCE) * q.size) {
      mark(r, 'not a close title match');
      continue;
    }
    const why = otherItemReason(r.title, query, itemText, mustHave, mustNotHave);
    if (why) { mark(r, why); continue; }
    r.related = versionMismatch(r.title, query, itemText, footwear);
  }

  // Price fences, per platform and per related version, on condition-adjusted log prices: a robust
  // centre and spread (median / MAD) flag anything 3 spreads away.
  const groups = new Map();
  for (const r of rows) groups.set(`${r.platform}\u0000${r.related}`, [r.platform, r.related]);
  for (const [platform, rel] of groups.values()) {
    const keep = rows.filter((r) => r.platform === platform && r.related === rel && !r.is_outlier);
    if (keep.length < MIN_FOR_FENCES) continue;
    const z = keep.map(adjLog);
    const centre = median(z);
    const spread = Math.max(1.4826 * median(z.map((v) => Math.abs(v - centre))), 0.15);
    keep.forEach((r, i) => {
      if (z[i] < centre - FENCE_K * spread) mark(r, 'price far below similar listings');
      else if (z[i] > centre + FENCE_K * spread) mark(r, 'price far above similar listings');
    });
  }
  // Against what actually sold: an exact match priced over 2.5x (or under 0.3x) the median
  // condition-adjusted sold price is a different item or a fantasy ask.
  const ref = rows.filter((r) => r.obs_type !== 'active' && !r.is_outlier && !r.related).map(adjLog);
  if (ref.length >= 3) {
    const mid = median(ref);
    for (const r of rows) {
      if (r.is_outlier || r.related) continue;
      const zi = adjLog(r) - mid;
      if (zi > Math.log(SOLD_HI)) mark(r, 'priced far above what this item sells for');
      else if (zi < Math.log(SOLD_LO)) mark(r, 'priced far below what this item sells for');
    }
  }
  return rows;
}
