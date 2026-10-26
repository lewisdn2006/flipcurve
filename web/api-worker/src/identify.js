// Identify an item from 1-4 photos (or a typed description) with Gemini -> ItemSpec.
// Port of backend/vision/gemini_identifier.py (NO_LAPTOP_PLAN.md, step 3); keep the two in step.
//
// Gemini is asked for strict JSON (response schema) describing the item, its condition on our
// four tiers, visible flaws, and two search queries written the way people search each site.
// Google sometimes answers 503 "high demand"; we retry with backoff, then try the backups (Claude,
// then other Gemini Flash models, then Gemma). An answer that can't be used (cut off, blocked, not
// JSON, no item named) also moves on to the next model rather than giving up.
//
// Differences from the Python:
// - Photos arrive already shrunk by the page (1280 px JPEG); a Worker can't resize images, so
//   there is no second round with smaller photos.
// - Models out of allowance are remembered in D1 (model_cooldowns), shared by every request.
// - One budget for the one-request routes: a Worker has no 100 s cut-off, so 180 s, as the
//   Python's background jobs had.

const API = 'https://generativelanguage.googleapis.com/v1beta';
const RETRY_STATUS = new Set([429, 500, 502, 503, 504]);
const BACKOFF_S = [1.0, 2.5, 5.0];       // per model; then the next model
export const BUDGET_S = 180.0;           // whole identify
const REQUEST_TIMEOUT_S = 75.0;
const GEMMA_TIMEOUT_S = 160.0;           // Gemma is slow on the free tier but has allowance left: let it take its time
const FIRST_MODEL_CAP_S = 40.0;          // longest wait for the first model, so the backups always get a turn
// models in the list that can't do this job (make images or speech, live audio, embeddings, agents)
const SKIP_IN_FALLBACK = ['image', 'tts', 'audio', 'live', 'embedding', 'thinking-exp', 'native', 'aqa', 'computer-use',
  'robotics', 'research', 'veo', 'imagen', 'lyria', 'translate', 'transcribe'];
const MIN_GEMMA_B = 4;                   // smaller Gemmas can't see photos well (or at all)
const DEFAULT_MODEL = 'gemini-3.8-flash';

export const PROMPT = `You are helping someone in the UK price a secondhand item they own, to sell on eBay or Vinted.
Look at the photo(s) (or read the description) and identify the exact product.

Return JSON only, matching the schema. Rules:
- title: how a seller would title it, e.g. "Nike Air Force 1 '07 Low White"
- brand, model_name, category, size_or_storage, era_or_year: null if you can't tell
- condition_tier: one of fair, good, very_good, mint (mint = new / new with tags)
- condition_score: 0-10, where 10 is brand new
- detected_flaws: short phrases for visible wear, e.g. "light creasing on toe box"; [] if none
- search_queries.ebay: 3-7 words, brand + model + key attribute, no size, e.g. "nike air force 1 07 white"
- search_queries.vinted: 2-5 casual words, e.g. "air force 1 white"
- must_have: 1-4 lowercase words that nearly every correct listing title includes, naming the model
  (not the brand alone, not the colour unless it's part of the name). Give alternatives with "|".
  e.g. Jordan 1 Low Triple Black -> ["jordan", "low"]; iPhone 13 Pro 128GB -> ["iphone 13", "pro"]
- must_not_have: lowercase words that would mean a different item: other colourways or
  collaborations of the same model, a different version, replicas. e.g. Jordan 1 Low Triple Black
  -> ["white", "red", "blue", "grey", "panda", "travis", "psg", "toe", "replica"]
- is_broken: true only if it looks broken or for parts
- confidence: 0-1, how sure you are of the exact product
`;

const SCHEMA = {
  type: 'OBJECT',
  properties: {
    title: { type: 'STRING' },
    brand: { type: 'STRING', nullable: true },
    model_name: { type: 'STRING', nullable: true },
    category: { type: 'STRING', nullable: true },
    size_or_storage: { type: 'STRING', nullable: true },
    era_or_year: { type: 'STRING', nullable: true },
    condition_tier: { type: 'STRING', enum: ['fair', 'good', 'very_good', 'mint'] },
    condition_score: { type: 'NUMBER' },
    detected_flaws: { type: 'ARRAY', items: { type: 'STRING' } },
    search_queries: { type: 'OBJECT', properties: { ebay: { type: 'STRING' }, vinted: { type: 'STRING' } },
      required: ['ebay', 'vinted'] },
    must_have: { type: 'ARRAY', items: { type: 'STRING' } },
    must_not_have: { type: 'ARRAY', items: { type: 'STRING' } },
    is_broken: { type: 'BOOLEAN' },
    confidence: { type: 'NUMBER' },
  },
  required: ['title', 'condition_tier', 'condition_score', 'detected_flaws', 'search_queries', 'confidence'],
};

const CLAUDE_URL = 'https://api.anthropic.com/v1/messages';
const CLAUDE_DEFAULT_MODEL = 'claude-haiku-5-5';   // fast, reads images; override with CLAUDE_MODEL
const CLAUDE_JSON_NOTE = '\nReply with only one JSON object (no other text) with these keys: title, brand, model_name, '
  + 'category, size_or_storage, era_or_year, condition_tier, condition_score, detected_flaws, '
  + 'search_queries {ebay, vinted}, must_have, must_not_have, is_broken, confidence.';

const claudeConfigured = (env) => Boolean((env.ANTHROPIC_API_KEY || '').trim());
export const prettyModel = (model) => (model.startsWith('claude:') ? 'Claude' : model);
const unique = (xs) => [...new Set(xs)];

/** Every model failed. .attempts = [[model, reason]] so the page can say what actually happened. */
export class IdentifyError extends Error {
  constructor(msg, attempts = [], env = {}) {
    super(msg);
    this.attempts = attempts;
    this.env = env;
  }

  /** Short, readable summary for the user, e.g. "gemini-3.8-flash: no answer within 40 s; Claude: busy (HTTP 529)". */
  reason() {
    if (!this.attempts.length) return this.message;
    const bits = this.attempts.map(([m, r]) => `${prettyModel(m)}: ${r}`);
    if (!claudeConfigured(this.env) && !this.attempts.some(([m]) => m.startsWith('gemma'))) {
      bits.push('no backup AI set up (add the ANTHROPIC_API_KEY secret, or GEMMA_MODEL)');
    }
    return bits.join('; ');
  }
}

/** Least thinking each model family allows: Gemini 3+ takes thinkingLevel; 2.5 Flash a thinkingBudget (0 = off). */
export function thinkingConfig(model) {
  const m = model.toLowerCase();
  if (m.includes('2.5') && m.includes('flash')) return { thinkingBudget: 0 };
  const v = m.match(/gemini-(\d+)/);
  if (v && Number(v[1]) >= 3) return { thinkingLevel: 'low' };
  return null;
}

export function sniffMime(bytes) {
  const ascii = (a, b) => String.fromCharCode(...bytes.slice(a, b));
  if (['ftypheic', 'ftypheix', 'ftypmif1', 'ftypmsf1', 'ftyphevc'].includes(ascii(4, 12))) return 'image/heic';
  if (bytes[0] === 0x89 && ascii(1, 4) === 'PNG') return 'image/png';
  if (ascii(0, 4) === 'RIFF' && ascii(8, 12) === 'WEBP') return 'image/webp';
  return 'image/jpeg';
}

/** The first bytes of base64 data: enough to tell the image type. */
const base64Head = (b64) => Uint8Array.from(atob(String(b64).slice(0, 24)), (ch) => ch.charCodeAt(0));

function base64(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

// ---- tolerant cleanup: models sometimes write "7/10", "85%", "high", a string instead of a list...

const CONF_WORDS = { 'very high': 0.95, high: 0.85, medium: 0.6, moderate: 0.6, low: 0.3, 'very low': 0.15 };
const TIER_WORDS = { 'very good': 'very_good', 'very-good': 'very_good', verygood: 'very_good', very_good: 'very_good',
  new: 'mint', 'brand new': 'mint', 'new with tags': 'mint', bnwt: 'mint', mint: 'mint',
  good: 'good', fair: 'fair', poor: 'fair', worn: 'fair' };

/** [number, unit] from 7.5, "7.5", "7/10", "7.5 out of 10", "85%"; unit is "", "%", "/10" or "word". */
function num(v) {
  if (typeof v === 'boolean' || v == null) return [null, ''];
  if (typeof v === 'number') return [v, ''];
  if (typeof v === 'string') {
    const s = v.trim().toLowerCase();
    if (s in CONF_WORDS) return [CONF_WORDS[s], 'word'];
    const m = s.match(/-?\d+(?:\.\d+)?/);
    if (!m) return [null, ''];
    const unit = s.includes('%') ? '%' : /\/\s*10\b|out of 10/.test(s) ? '/10' : '';
    return [Number(m[0]), unit];
  }
  return [null, ''];
}

function score(v) {
  let [x, unit] = num(v);
  if (x === null || unit === 'word') return 6.0;
  if (unit === '%') x /= 10;
  return Math.min(Math.max(x, 0), 10);
}

function confidence(v) {
  let [x, unit] = num(v);
  if (x === null) return null;
  if (unit === '%') x /= 100;
  else if (unit === '/10') x /= 10;
  return Math.min(Math.max(x, 0), 1);
}

const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

function str(v) {
  if (v == null || isObject(v) || typeof v === 'boolean') return null;
  if (Array.isArray(v)) v = v.filter((x) => x != null).map(String).join(', ');
  const s = String(v).trim();
  return ['', 'null', 'none', 'n/a', 'unknown'].includes(s.toLowerCase()) ? null : s;
}

function strs(v) {
  if (v == null || ['boolean', 'number'].includes(typeof v) || isObject(v)) return [];
  if (typeof v === 'string') return v.split(/[;,\n]/).map((t) => t.trim()).filter(Boolean);
  return v.filter((x) => x != null && String(x).trim()).map((x) => String(x).trim());
}

function terms(v, n) {
  const out = [];
  for (const raw of strs(v).slice(0, n)) {
    const t = raw.toLowerCase().split(/\s+/).filter(Boolean).join(' ');
    if (t && !out.includes(t)) out.push(t);
  }
  return out;
}

const tier = (v) => TIER_WORDS[String(v ?? '').toLowerCase().replaceAll('_', ' ').split(/\s+/).filter(Boolean).join(' ')] || 'good';

/** Coerce the model's JSON into a valid ItemSpec (+ confidence). Never throws on odd values. */
export function clean(raw) {
  const title = str(raw.title) || str([str(raw.brand), str(raw.model_name)].filter(Boolean).join(' ')) || 'Unknown item';
  let q = raw.search_queries;
  if (typeof q === 'string') q = { ebay: q, vinted: q };
  q = isObject(q) ? q : {};
  const ebayQ = (str(q.ebay) || title).toLowerCase();
  const vintedQ = (str(q.vinted) || ebayQ).toLowerCase();
  const item = {
    title, brand: str(raw.brand), model_name: str(raw.model_name),
    category: str(raw.category), size_or_storage: str(raw.size_or_storage),
    era_or_year: str(raw.era_or_year), condition_tier: tier(raw.condition_tier),
    condition_score: score(raw.condition_score),
    detected_flaws: strs(raw.detected_flaws).slice(0, 6),
    search_queries: { ebay: ebayQ, vinted: vintedQ },
    is_broken: raw.is_broken === true || String(raw.is_broken).toLowerCase() === 'true',
    must_have: terms(raw.must_have, 4),
    must_not_have: terms(raw.must_not_have, 15),
  };
  return { item, confidence: confidence(raw.confidence) };
}

/** The model answered, but not with something we can use. */
export class Unusable extends Error {
  constructor(reason, partial = null) {
    super(reason);
    this.partial = partial;
  }
}

const BLOCKED = new Set(['SAFETY', 'RECITATION', 'PROHIBITED_CONTENT', 'BLOCKLIST', 'SPII', 'IMAGE_SAFETY', 'OTHER']);

/** Model reply (Gemini's response shape) -> cleaned item, or Unusable with a readable reason. */
export function parseAnswer(resp) {
  if (!isObject(resp)) throw new Unusable('empty answer');
  const block = (resp.promptFeedback || {}).blockReason;
  if (block) throw new Unusable(`answer blocked (${block})`);
  const cands = resp.candidates || [];
  if (!cands.length) throw new Unusable('empty answer');
  const cand = cands[0] || {};
  const finish = cand.finishReason || '';
  // thinking parts (thought: true) are the model's notes, not its answer
  const text = ((cand.content || {}).parts || []).filter((p) => isObject(p) && !p.thought).map((p) => p.text || '').join('');
  if (!text.trim()) {
    if (BLOCKED.has(finish)) throw new Unusable(`answer blocked (${finish})`);
    if (finish === 'MAX_TOKENS') throw new Unusable('ran out of room before answering');
    throw new Unusable('empty answer');
  }
  const raw = findJson(text);
  if (raw === null) {
    let shown = text.split(/\s+/).filter(Boolean).join(' ');
    shown = shown.slice(0, 400) + (shown.length > 400 ? '…' : '');
    const why = finish === 'MAX_TOKENS' ? 'answer was cut off' : "answer wasn't valid JSON";
    throw new Unusable(`${why} (it said: «${shown}»)`);
  }
  const out = clean(raw);
  const q = raw.search_queries;
  const named = str(raw.title) || str(raw.model_name) || str(isObject(q) ? q.ebay : q);
  if (!named) throw new Unusable("couldn't name the item", out);  // another model may do better; kept as a last resort
  return out;
}

const ITEM_KEYS = ['title', 'search_queries', 'condition_tier', 'brand', 'model_name'];

/** Places the JSON might be: the whole reply, ```fenced``` blocks, then every balanced {...}
 *  (last first: models tend to put notes first and the answer last). */
function* jsonCandidates(text) {
  const t = text.trim();
  yield t;
  for (const m of t.matchAll(/```(?:json|JSON)?\s*([\s\S]*?)```/g)) yield m[1].trim();
  const objs = [];
  let depth = 0, start = null, inStr = false, esc = false;
  for (let i = 0; i < t.length; i++) {
    const ch = t[i];
    if (inStr) {
      if (esc) esc = false;
      else if (ch === '\\') esc = true;
      else if (ch === '"') inStr = false;
      continue;
    }
    if (ch === '"') inStr = true;
    else if (ch === '{') {
      if (depth === 0) start = i;
      depth++;
    } else if (ch === '}' && depth) {
      depth--;
      if (depth === 0) objs.push(t.slice(start, i + 1));
    }
  }
  yield* objs.reverse();
  if (start !== null && depth) yield t.slice(start) + '}'.repeat(depth);  // an object that never closed (cut off)
}

/** Common slips: comments, trailing commas, curly quotes, Python's True/False/None. */
function repair(t) {
  return t.replace(/\/\/[^\n"]*$/gm, '')
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/“|”/g, '"').replace(/‘|’/g, "'")
    .replace(/,\s*([}\]])/g, '$1')
    .replace(/\bNone\b/g, 'null').replace(/\bFalse\b/g, 'false').replace(/\bTrue\b/g, 'true');
}

/** Python-style literal (single-quoted strings, True/False/None) -> JSON text; stands in for ast.literal_eval. */
function pythonLiteralToJson(t) {
  let out = '';
  for (let i = 0; i < t.length; i++) {
    const ch = t[i];
    if (ch === '"' || ch === "'") {
      let j = i + 1, s = '';
      for (; j < t.length && t[j] !== ch; j++) {
        if (t[j] === '\\' && j + 1 < t.length) {
          j++;
          s += { n: '\n', t: '\t', r: '\r' }[t[j]] ?? t[j];
        } else s += t[j];
      }
      if (j >= t.length) throw new SyntaxError('unclosed string');
      out += JSON.stringify(s);
      i = j;
    } else if (/[A-Za-z_]/.test(ch)) {
      const word = t.slice(i).match(/^[A-Za-z_]\w*/)[0];
      const lit = { True: 'true', False: 'false', None: 'null' }[word];
      if (lit === undefined) throw new SyntaxError(`unexpected name ${word}`);
      out += lit;
      i += word.length - 1;
    } else out += ch;
  }
  return out;
}

/** The item object in a model's reply, however it was wrapped; null if there isn't one. */
export function findJson(text) {
  let first = null;
  for (const c of jsonCandidates(text)) {
    for (const attempt of [c, repair(c)]) {
      let v;
      try {
        v = JSON.parse(attempt);
      } catch {
        try {
          v = JSON.parse(pythonLiteralToJson(attempt));  // single-quoted keys and strings
        } catch {
          continue;
        }
      }
      if (Array.isArray(v) && v.length && isObject(v[0])) v = v[0];
      if (isObject(v)) {
        if (ITEM_KEYS.some((k) => k in v)) return v;
        first = first || v;
      }
    }
  }
  return first;
}

/** Google's own error message, in full (it's what tells you which limit or which problem). */
function googleError(r) {
  const msg = r.json?.error?.message;
  return msg != null ? String(msg).split(/\s+/).filter(Boolean).join(' ') : r.text.split(/\s+/).filter(Boolean).join(' ').slice(0, 600);
}

// ---- models that are out of allowance (HTTP 429) are skipped until their limit resets, instead of
// being asked again on every identify. Kept in D1 so every request (and the next deploy) sees them.

/** Cooldowns in the model_cooldowns table: load() once per identify, then put/remove as they change. */
export class D1Cooldowns {
  constructor(db) {
    this.db = db;
  }

  async load() {
    const { results } = await this.db.prepare('SELECT model, until, why, since FROM model_cooldowns').all();
    return new Map(results.map((r) => [r.model, { until: r.until, why: r.why, since: r.since }]));
  }

  async put(model, c) {
    await this.db.prepare(`INSERT INTO model_cooldowns (model, until, why, since) VALUES (?, ?, ?, ?)
      ON CONFLICT (model) DO UPDATE SET until = excluded.until, why = excluded.why, since = excluded.since`)
      .bind(model, c.until, c.why, c.since).run();
  }

  async remove(model) {
    await this.db.prepare('DELETE FROM model_cooldowns WHERE model = ?').bind(model).run();
  }
}

/** For tests, and for running without a database. */
export class MemoryCooldowns {
  constructor(entries = {}) {
    this.map = new Map(Object.entries(entries));
  }

  async load() { return new Map(this.map); }
  async put(model, c) { this.map.set(model, c); }
  async remove(model) { this.map.delete(model); }
}

/** Wall-clock parts of a moment in a time zone. */
function zoned(ts, timeZone) {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone, hourCycle: 'h23', year: 'numeric', month: '2-digit',
    day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).formatToParts(new Date(ts * 1000));
  const p = Object.fromEntries(parts.map((x) => [x.type, Number(x.value)]));
  return { y: p.year, mo: p.month, d: p.day, h: p.hour, mi: p.minute, s: p.second };
}

/** Gemini API daily limits reset at midnight Pacific time (5 s after, to be safe). */
export function nextPacificMidnight(now) {
  const tz = 'America/Los_Angeles';
  const t = zoned(now, tz);
  const target = Date.UTC(t.y, t.mo - 1, t.d + 1, 0, 0, 5) / 1000;   // tomorrow 00:00:05 as if it were UTC
  let guess = target + 8 * 3600;
  for (let i = 0; i < 2; i++) {  // correct by the zone's offset at the guess (PST or PDT)
    const g = zoned(guess, tz);
    guess += target - Date.UTC(g.y, g.mo - 1, g.d, g.h, g.mi, g.s) / 1000;
  }
  return guess;
}

/** "14:05", or "00:00 tomorrow", in UK time (where the seller is). */
export function clock(ts, now) {
  const a = zoned(ts, 'Europe/London'), b = zoned(now, 'Europe/London');
  const hhmm = `${String(a.h).padStart(2, '0')}:${String(a.mi).padStart(2, '0')}`;
  return hhmm + (Date.UTC(a.y, a.mo - 1, a.d) > Date.UTC(b.y, b.mo - 1, b.d) ? ' tomorrow' : '');
}

/** From a 429 body: which limit (quotaId / metric), the per-day flag, and Google's retry delay. */
export function quotaDetails(r) {
  const out = { ids: [], daily: false, retryS: null };
  for (const d of r.json?.error?.details || []) {
    const t = d['@type'] || '';
    if (t.endsWith('QuotaFailure')) {
      for (const v of d.violations || []) {
        const qid = v.quotaId || v.quotaMetric || '';
        if (qid) out.ids.push(qid + (v.quotaValue ? ` (limit ${v.quotaValue})` : ''));
        if (/per ?day/i.test(qid)) out.daily = true;
      }
    } else if (t.endsWith('RetryInfo')) {
      const m = String(d.retryDelay || '').match(/^([\d.]+)s/);
      if (m) out.retryS = Number(m[1]);
    }
  }
  if (!out.ids.length && /per ?day|daily/i.test(r.text)) out.daily = true;
  return out;
}

// Models this key can use, kept for an hour per Worker instance: Google's list is large, and parsing it
// on every identify costs CPU the free plan doesn't have.
const MODEL_LIST_TTL_MS = 3600000;
let modelListCache = null;  // {key, names, at}

/** One HTTP call; a timeout throws {name: "TimeoutError"}, other network failures throw too. */
async function fetchTransport({ url, method, headers, body, timeoutS }) {
  return fetch(url, { method, headers, body, signal: AbortSignal.timeout(Math.max(1, timeoutS) * 1000) });
}

export class Identifier {
  /**
   * env: the Worker's env (GEMINI_API_KEY, GEMINI_MODEL, GEMMA_MODEL, GEMINI_FALLBACK_MODELS, PHOTO_AI_FIRST,
   * ANTHROPIC_API_KEY, CLAUDE_MODEL, ANTHROPIC_VERSION). transport, sleep and now are for tests.
   */
  constructor({ env = {}, cooldowns = new MemoryCooldowns(), transport = fetchTransport,
    sleep = (s) => new Promise((r) => setTimeout(r, s * 1000)), now = () => Date.now() / 1000,
    budgetS = BUDGET_S, model = null } = {}) {
    this.env = env;
    this.key = env.GEMINI_API_KEY || '';
    this.model = model || env.GEMINI_MODEL || DEFAULT_MODEL;
    this.cooldowns = cooldowns;
    this.transport = transport;
    this.sleep = sleep;
    this.now = now;
    this.budgetS = budgetS;
    this.deadline = null;
    this.lightThinking = true;
    this.usedModel = null;
    this.lead = null;                // the model asked first on purpose (PHOTO_AI_FIRST), not a backup
    this.names = null;               // models list, fetched at most once
    this.noJsonMode = new Set();     // models that refused the JSON response setting
    this.cooling = null;             // model -> cooldown, loaded at the start of each identify
    if (!this.key && !claudeConfigured(env)) throw new IdentifyError('GEMINI_API_KEY is not set', [], env);
  }

  async request(req) {
    const r = await this.transport(req);
    const text = await r.text();
    let json = null;
    try { json = JSON.parse(text); } catch { /* not JSON */ }
    return { status: r.status, text, json };
  }

  /** Models this key can call generateContent on (asked at most once an hour). Never throws. */
  async listModels() {
    if (this.names !== null) return this.names;
    this.names = [];
    if (!this.key) return this.names;
    if (modelListCache && modelListCache.key === this.key && Date.now() - modelListCache.at < MODEL_LIST_TTL_MS) {
      this.names = modelListCache.names;
      return this.names;
    }
    try {
      const r = await this.request({ url: `${API}/models?pageSize=200`, method: 'GET',
        headers: { 'x-goog-api-key': this.key }, timeoutS: Math.min(10, Math.max(1, this.left() - 2)) });
      if (r.status === 200) {
        this.names = (r.json?.models || []).filter((m) => (m.supportedGenerationMethods || []).includes('generateContent'))
          .map((m) => m.name.split('/').slice(1).join('/') || m.name);
        if (this.transport === fetchTransport) modelListCache = { key: this.key, names: this.names, at: Date.now() };
      }
    } catch { /* no list: just the main model */ }
    return this.names;
  }

  /** Order to try models in: Flash (newest first), Flash-Lite, Pro, then Gemma (newest, largest;
   *  slow but with the biggest free allowance), then anything else. */
  static rank(n) {
    const v = n.match(/(?:gemini|gemma)-(\d+(?:\.\d+)?)/);
    const ver = v ? Number(v[1]) : 0;
    const size = n.replaceAll('-', ' ').match(/(\d+)b\b/);
    const b = size ? Number(size[1]) : 0;
    const fam = n.startsWith('gemma') ? 3 : n.includes('lite') ? 1 : n.includes('flash') ? 0 : n.includes('pro') ? 2 : 4;
    return [fam, -ver, -b, n];
  }

  static byRank(a, b) {
    const x = Identifier.rank(a), y = Identifier.rank(b);
    for (let i = 0; i < x.length; i++) if (x[i] !== y[i]) return x[i] < y[i] ? -1 : 1;
    return 0;
  }

  gemmaOk(n) {
    const size = n.replaceAll('-', ' ').match(/(\d+)b\b/);
    return n.startsWith('gemma') && Boolean(size) && Number(size[1]) >= MIN_GEMMA_B;
  }

  /** Best Gemma: GEMMA_MODEL pins one ("" turns Gemma off); otherwise the newest, largest this key can use. */
  async gemmaModel() {
    const env = this.env.GEMMA_MODEL;
    if (env !== undefined) return env.trim() || null;
    if (this.env.GEMINI_FALLBACK_MODELS !== undefined) return null;
    const gs = (await this.listModels()).filter((n) => this.gemmaOk(n) && !SKIP_IN_FALLBACK.some((k) => n.includes(k)))
      .sort(Identifier.byRank);
    return gs[0] || null;
  }

  /** Every other model this key can use for the job, best first. Ones out of allowance are skipped later
   *  by their cooldown. GEMINI_FALLBACK_MODELS (comma-separated) replaces the list; GEMMA_MODEL pins or turns off Gemma. */
  async fallbackModels() {
    if (!this.key) return [];
    const env = this.env.GEMINI_FALLBACK_MODELS;
    let out;
    if (env !== undefined) {
      out = env.split(',').map((m) => m.trim()).filter((m) => m && m !== this.model);
    } else {
      out = (await this.listModels()).filter((n) => n !== this.model && !SKIP_IN_FALLBACK.some((k) => n.includes(k))
        && (n.startsWith('gemini') || this.gemmaOk(n)));
      if (this.env.GEMMA_MODEL !== undefined) out = out.filter((n) => !n.startsWith('gemma'));  // pinned replaces found
      out = unique(out).sort(Identifier.byRank);
    }
    const g = (this.env.GEMMA_MODEL || '').trim() || (env !== undefined ? await this.gemmaModel() : null);
    return out.concat(g && !out.includes(g) && g !== this.model ? [g] : []);
  }

  left() {
    return this.deadline ? this.deadline - this.now() : this.budgetS;
  }

  async start() {
    this.deadline = this.now() + this.budgetS;
    try {
      this.cooling = await this.cooldowns.load();
    } catch (e) {
      console.warn('cooldowns unavailable:', e.message);
      this.cooling = new Map();
    }
  }

  async coolingFor(model) {
    const c = this.cooling.get(model);
    if (c && c.until <= this.now()) {
      this.cooling.delete(model);
      await this.cooldowns.remove(model).catch(() => {});
      return null;
    }
    return c || null;
  }

  /** Remember that `model` is out of allowance; returns a description of when it's tried again. */
  async coolDown(model, r) {
    const now = this.now();
    const q = quotaDetails(r);
    const until = q.daily ? nextPacificMidnight(now) : now + Math.max(q.retryS || 60, 10);
    const why = (q.daily ? 'daily limit' : 'per-minute limit') + (q.ids.length ? `: ${q.ids.join(', ')}` : '');
    const c = { until, why, since: now };
    this.cooling.set(model, c);
    await this.cooldowns.put(model, c).catch((e) => console.warn('couldn\'t save cooldown:', e.message));
    return `${why}; skipped until ${clock(until, now)}`;
  }

  /** Try models in turn until one gives a usable answer -> cleaned item. Order: the main Gemini model
   *  (or Claude / Gemma first if asked), then Claude, then other Flash models, then Gemma. */
  async call(parts, { useSchema = true, first = 'gemini' } = {}) {
    if (this.deadline === null) await this.start();
    const claude = claudeConfigured(this.env) ? `claude:${this.env.CLAUDE_MODEL || CLAUDE_DEFAULT_MODEL}` : null;
    const main = this.key ? this.model : null;
    const lead = first === 'claude' ? claude : first === 'gemma' && this.key ? await this.gemmaModel() : null;
    this.lead = lead;
    let queue = unique([lead, main]).filter(Boolean);
    const attempts = [], tried = [];
    let partial = null, geminiOk = Boolean(this.key), extended = false;
    for (;;) {
      if (!queue.length) {
        if (extended) break;
        extended = true;
        queue = [claude, ...(geminiOk ? await this.fallbackModels() : [])].filter((m) => m && !tried.includes(m));
        if (!queue.length) break;
        continue;
      }
      const model = queue.shift();
      if (this.left() < 5) {
        attempts.push([model, 'out of time']);
        break;
      }
      const c = model.startsWith('claude:') ? null : await this.coolingFor(model);
      if (c) {
        attempts.push([model, `skipped: usage limit reached at ${clock(c.since, this.now())} (${c.why}); `
          + `tried again after ${clock(c.until, this.now())}`]);
        continue;
      }
      tried.push(model);
      // the first model can't use up the whole budget, so the backups always get a turn.
      // Not Gemma: it's slow but often the only one with allowance left, so it gets the time.
      const cap = tried.length === 1 && !model.startsWith('gemma') ? FIRST_MODEL_CAP_S : null;
      const [resp, err, kind] = await this.callModel(model, parts, useSchema, cap);
      if (resp !== null) {
        try {
          const out = parseAnswer(resp);
          this.usedModel = model;
          return out;
        } catch (u) {
          if (!(u instanceof Unusable)) throw u;
          attempts.push([model, u.message]);
          partial = partial || u.partial;
          continue;
        }
      }
      attempts.push([model, err]);
      if (kind === 'auth' && !model.startsWith('claude:')) {
        geminiOk = false;  // a refused Gemini key won't work for other Gemini models either
        queue = queue.filter((m) => m.startsWith('claude:'));
      }
    }
    if (partial) {  // nobody named the item: keep the best half-answer rather than nothing
      this.usedModel = tried[0] || null;
      return partial;
    }
    console.warn('identify failed:', JSON.stringify(attempts));
    throw new IdentifyError(`All models failed (${attempts.map(([m, r]) => `${m}: ${r}`).join('; ')})`, attempts, this.env);
  }

  /** Same request to Anthropic's Messages API; the answer comes back in Gemini's response shape so
   *  the rest of the code doesn't care which AI answered. */
  async callClaude(model, parts, cap) {
    const content = [];
    let wanted = 0;
    for (const p of parts) {
      if (p.inline_data) {
        wanted++;
        const mime = p.inline_data.mime_type;
        if (!['image/jpeg', 'image/png', 'image/gif', 'image/webp'].includes(mime)) continue;  // e.g. HEIC
        content.push({ type: 'image', source: { type: 'base64', media_type: mime, data: p.inline_data.data } });
      }
    }
    if (wanted && !content.length) return [null, "couldn't read the photo format", 'error'];  // don't let it guess from the text alone
    content.push({ type: 'text', text: parts.filter((p) => 'text' in p).map((p) => p.text).join(' ') + CLAUDE_JSON_NOTE });
    const left = this.left();
    if (left < 5) return [null, 'out of time', 'busy'];
    const timeout = Math.min(REQUEST_TIMEOUT_S, left - 2, cap || REQUEST_TIMEOUT_S);
    let r;
    try {
      r = await this.request({ url: CLAUDE_URL, method: 'POST', timeoutS: timeout,
        body: JSON.stringify({ model, max_tokens: 1500, messages: [{ role: 'user', content }] }),
        headers: { 'x-api-key': this.env.ANTHROPIC_API_KEY || '', 'anthropic-version': this.env.ANTHROPIC_VERSION || '2023-06-01',
          'content-type': 'application/json' } });
    } catch (e) {
      if (e?.name === 'TimeoutError') return [null, `no answer within ${Math.round(timeout)} s`, 'busy'];
      return [null, `network error (${e?.name || 'Error'})`, 'busy'];
    }
    if (r.status !== 200) {
      const kind = RETRY_STATUS.has(r.status) || r.status === 529 ? 'busy' : [401, 403].includes(r.status) ? 'auth' : 'error';
      const label = { busy: r.status === 429 ? 'usage limit reached' : 'busy', auth: 'API key refused' }[kind] || 'rejected the request';
      return [null, kind === 'error' ? `${label} (HTTP ${r.status}: ${googleError(r)})` : `${label} (HTTP ${r.status})`, kind];
    }
    const body = r.json || {};
    const out = (body.content || []).filter((b) => b.type === 'text').map((b) => b.text || '').join('');
    if (model !== this.model) console.warn(`Gemini ${this.model} didn't answer; used Claude ${model}`);
    const finish = body.stop_reason === 'max_tokens' ? 'MAX_TOKENS' : 'STOP';
    return [{ candidates: [{ content: { parts: [{ text: out }] }, finishReason: finish }] }, '', 'ok'];
  }

  /** [response, last error, kind] for one model, with retries and backoff.
   *  kind: ok | busy (429/5xx/slow/network) | auth (key refused) | error (anything else). */
  async callModel(model, parts, useSchema, cap = null) {
    if (model.startsWith('claude:')) return this.callClaude(model.split(':').slice(1).join(':'), parts, cap);
    // Gemma (and any model that refused it before) doesn't take the JSON response setting: ask for
    // JSON in the prompt instead, as with Claude.
    const jsonMode = !model.startsWith('gemma') && !this.noJsonMode.has(model);
    const cfg = { temperature: 0.2 };
    if (jsonMode) {
      cfg.responseMimeType = 'application/json';
      if (useSchema) cfg.responseSchema = SCHEMA;
    }  // otherwise the prompt asks for JSON instead (partsJson's "noted" variant)
    const think = this.lightThinking ? thinkingConfig(model) : null;
    // Gemini 3 Flash thinks at "high" by default, which can take a long time before it answers;
    // naming a product doesn't need it.
    if (think) cfg.thinkingConfig = think;
    // the photos are most of the request: their JSON is made once per identify and spliced in
    const body = `{"contents":[{"role":"user","parts":${this.partsJson(parts, !jsonMode)}}],"generationConfig":${JSON.stringify(cfg)}}`;
    const url = `${API}/models/${model}:generateContent`;
    let last = '', kind = 'error';
    const tEnd = cap ? this.now() + cap : null;  // the cap covers retries too
    for (let attempt = 0; attempt <= BACKOFF_S.length; attempt++) {
      const left = tEnd ? Math.min(this.left(), tEnd - this.now() + 2) : this.left();
      if (left < 5) {
        return [null, (last ? last + ' ' : '') + (tEnd ? '(gave up to try the backups)' : '(out of time)'), 'busy'];
      }
      const timeout = Math.min(model.startsWith('gemma') ? GEMMA_TIMEOUT_S : REQUEST_TIMEOUT_S, left - 2);
      let r = null;
      try {
        r = await this.request({ url, method: 'POST', body, timeoutS: timeout,
          headers: { 'x-goog-api-key': this.key, 'content-type': 'application/json' } });
      } catch (e) {
        if (e?.name === 'TimeoutError') return [null, `no answer within ${Math.round(timeout)} s`, 'busy'];  // slow: try another model
        last = `network error (${e?.name || 'Error'})`;
        kind = 'busy';
      }
      if (r) {
        const lower = r.text.toLowerCase();
        if (r.status === 200) {
          if (model !== this.model) console.warn(`Gemini ${this.model} didn't answer; used ${model}`);
          return [r.json, '', 'ok'];
        }
        if (r.status === 400 && this.lightThinking && lower.includes('think')) {
          this.lightThinking = false;  // model without that setting: ask again without it
          return this.callModel(model, parts, useSchema, cap);
        }
        if (r.status === 400 && jsonMode && /json mode|response_?mime_?type/.test(lower)) {
          this.noJsonMode.add(model);  // model without the JSON setting: ask in the prompt instead
          return this.callModel(model, parts, useSchema, cap);
        }
        if (r.status === 400 && jsonMode && useSchema && lower.includes('schema')) {
          return this.callModel(model, parts, false, cap);  // model without schema support
        }
        if (r.status === 429) {
          // not "busy": this key has used up its allowance (requests per minute or per day).
          // Skip this model until the limit resets; retrying now only spends more of it.
          const when = await this.coolDown(model, r);
          last = `usage limit reached (HTTP 429: ${googleError(r)}) [${when}]`;
          kind = 'busy';
          break;
        } else if (RETRY_STATUS.has(r.status)) {
          last = `busy (HTTP ${r.status})`;
          kind = 'busy';
        } else if ([401, 403].includes(r.status)) {
          return [null, `API key refused (HTTP ${r.status})`, 'auth'];
        } else {
          return [null, `rejected the request (HTTP ${r.status}: ${googleError(r)})`, 'error'];
        }
      }
      if (attempt < BACKOFF_S.length) {
        const leftNow = tEnd ? Math.min(this.left(), tEnd - this.now()) : this.left();
        if (leftNow - BACKOFF_S[attempt] < 8) break;  // not enough time for another go: try the next model instead
        await this.sleep(BACKOFF_S[attempt]);
      }
    }
    return [null, last, kind];
  }

  /** JSON text of the request parts, made once per parts list and prompt variant ("noted": the prompt
   *  asks for JSON, for models without the JSON setting, as with Claude). */
  partsJson(parts, noted) {
    this.partsCache ||= new WeakMap();
    let c = this.partsCache.get(parts);
    if (!c) this.partsCache.set(parts, (c = {}));
    const k = noted ? 'noted' : 'plain';
    return (c[k] ??= JSON.stringify(noted ? parts.map((p, i) => (i === 0 && 'text' in p ? { ...p, text: p.text + CLAUDE_JSON_NOTE } : p)) : parts));
  }

  /** images: [Uint8Array], or [{data: base64, mime_type?}] as the page sends them (already shrunk);
   *  hint: optional text from the seller. */
  async fromImages(images, hint = null) {
    await this.start();
    const first = (this.env.PHOTO_AI_FIRST || 'gemini').trim().toLowerCase();  // gemini | claude | gemma
    const parts = [{ text: PROMPT + (hint ? `\nThe seller adds: ${hint}` : '') }];
    for (const img of images.slice(0, 4)) {
      if (img instanceof Uint8Array) parts.push({ inline_data: { mime_type: sniffMime(img), data: base64(img) } });
      else parts.push({ inline_data: { mime_type: sniffMime(base64Head(img.data)), data: img.data } });
    }
    const out = await this.call(parts, { first });
    return { ...out, model: this.usedModel, fallback: ![this.model, this.lead].includes(this.usedModel) };
  }

  async fromText(query, conditionTier = null) {
    await this.start();
    const extra = conditionTier ? `\nThe seller says the condition is: ${conditionTier}.` : '';
    const out = await this.call([{ text: PROMPT + `\nDescription: ${query}${extra}` }]);
    if (conditionTier) out.item.condition_tier = conditionTier;
    return { ...out, model: this.usedModel, fallback: this.usedModel !== this.model };
  }
}
