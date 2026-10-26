// Port of tests/test_identifier.py: the identifier against canned API responses (no network).
// Left out: photo shrinking and the smaller-photo retry (the page shrinks photos; see identify.js).
import { describe, expect, it } from 'vitest';
import { env, exports } from 'cloudflare:workers';
import { D1Cooldowns, findJson, Identifier, IdentifyError, MemoryCooldowns, nextPacificMidnight, thinkingConfig }
  from '../src/identify.js';

const GOOD = { title: "Nike Air Force 1 '07 Low White", brand: 'Nike', model_name: "Air Force 1 '07",
  category: 'Trainers', size_or_storage: 'UK 9', era_or_year: null, condition_tier: 'good',
  condition_score: 7.5, detected_flaws: ['toe box creasing'],
  search_queries: { ebay: 'Nike Air Force 1 07 White', vinted: 'air force 1 white' },
  is_broken: false, confidence: 0.9 };

const respond = (status, body) => new Response(typeof body === 'string' ? body : JSON.stringify(body), { status });
const reply = (obj) => respond(200, { candidates: [{ content: { parts: [{ text: JSON.stringify(obj) }] } }] });
const textReply = (text, parts = null) => respond(200, { candidates: [{ content: { parts: parts || [{ text }] } }] });
const claudeOk = (obj = GOOD) => respond(200, { type: 'message', stop_reason: 'end_turn',
  content: [{ type: 'text', text: JSON.stringify(obj) }] });
const modelOf = (req) => (new URL(req.url).host === 'api.anthropic.com' ? 'claude'
  : new URL(req.url).pathname.split('/models/')[1].split(':')[0]);
const timeoutError = () => new DOMException('slow', 'TimeoutError');
const JPEG = new Uint8Array([0xff, 0xd8, 0xff, 0xe0, 0, 16, 74, 70, 73, 70]);
const HEIC = new Uint8Array([0, 0, 0, 24, ...new TextEncoder().encode('ftypheic'), 0, 0]);

/** Like the Python tests' make(): key "k", main model "m", no real waiting. */
function make(handler, { env: extra = {}, budgetS = 70, now, cooldowns = new MemoryCooldowns(), key = 'k' } = {}) {
  const clock = now || (() => 1_000_000);
  return new Identifier({ env: { GEMINI_API_KEY: key, ...extra }, model: 'm', budgetS, cooldowns, now: clock,
    transport: async (req) => handler(req), sleep: async () => {} });
}

async function fails(promise) {
  try {
    await promise;
  } catch (e) {
    expect(e).toBeInstanceOf(IdentifyError);
    return e;
  }
  throw new Error('expected IdentifyError');
}

/** Python's json.dumps(v): spaces after ":" and ",", so cutting N characters off lands where the Python test's cut did. */
function pyDumps(v) {
  if (Array.isArray(v)) return `[${v.map(pyDumps).join(', ')}]`;
  if (v !== null && typeof v === 'object') return `{${Object.entries(v).map(([k, x]) => `${JSON.stringify(k)}: ${pyDumps(x)}`).join(', ')}}`;
  return JSON.stringify(v);
}

/** Python's str(dict), for the "Python-style dict" answer. */
function pyRepr(v) {
  if (v === null) return 'None';
  if (v === true) return 'True';
  if (v === false) return 'False';
  if (typeof v === 'number') return Number.isInteger(v) ? `${v}` : String(v);
  if (typeof v === 'string') return v.includes("'") && !v.includes('"') ? `"${v}"` : `'${v.replaceAll("'", "\\'")}'`;
  if (Array.isArray(v)) return `[${v.map(pyRepr).join(', ')}]`;
  return `{${Object.entries(v).map(([k, x]) => `${pyRepr(k)}: ${pyRepr(x)}`).join(', ')}}`;
}

describe('basics', () => {
  it('text identify returns a valid item spec', async () => {
    const out = await make(() => reply(GOOD)).fromText('air force 1 white size 9');
    expect(out.item.search_queries.ebay).toBe('nike air force 1 07 white');
    expect(out.confidence).toBe(0.9);
    expect(Object.keys(out.item).sort()).toEqual(['brand', 'category', 'condition_score', 'condition_tier', 'detected_flaws',
      'era_or_year', 'is_broken', 'model_name', 'must_have', 'must_not_have', 'search_queries', 'size_or_storage', 'title']);
  });

  it('image identify sends the photo inline, with the hint', async () => {
    const seen = {};
    await make((req) => {
      const body = JSON.parse(req.body);
      seen.parts = body.contents[0].parts;
      expect(body.generationConfig.responseMimeType).toBe('application/json');
      return reply(GOOD);
    }).fromImages([JPEG], 'size 9');
    expect(seen.parts[1].inline_data.mime_type).toBe('image/jpeg');
    expect(seen.parts[0].text).toContain('size 9');
  });

  it('retries on busy, then succeeds', async () => {
    let n = 0;
    await make(() => (++n < 3 ? respond(503, 'high demand') : reply(GOOD))).fromText('air force 1');
    expect(n).toBe(3);
  });

  it('gives up with a clear error', async () => {
    await fails(make(() => respond(503, 'high demand')).fromText('air force 1'));
  });

  it('cleans messy output', async () => {
    const messy = { title: '', condition_tier: 'excellent', condition_score: 14, detected_flaws: null, search_queries: {}, confidence: 3 };
    const out = await make(() => reply(messy)).fromText('thing');
    expect(out.item.condition_tier).toBe('good');
    expect(out.item.condition_score).toBe(10);
    expect(out.confidence).toBe(1);
  });

  it('reads odd values instead of crashing', async () => {
    const odd = { title: "Levi's 501 Jeans", condition_tier: 'Very Good', condition_score: '7/10',
      detected_flaws: 'faded knees; small mark on pocket', search_queries: 'levis 501',
      must_have: '501', is_broken: 'false', confidence: '85%', brand: ["Levi's"] };
    const it_ = (await make(() => reply(odd)).fromText('levis'));
    expect(it_.item).toMatchObject({ condition_tier: 'very_good', condition_score: 7, detected_flaws: ['faded knees', 'small mark on pocket'],
      must_have: ['501'], is_broken: false, brand: "Levi's" });
    expect(it_.item.search_queries.vinted).toBe('levis 501');
    expect(it_.confidence).toBe(0.85);
    for (const [conf, want] of [['high', 0.85], ['8/10', 0.8], [null, null]]) {
      expect((await make(() => reply({ ...GOOD, confidence: conf })).fromText('x')).confidence).toBe(want);
    }
  });
});

describe('moving on to other models', () => {
  it('switches to another Flash model when busy', async () => {
    const tried = [];
    const out = await make((req) => {
      if (req.method === 'GET') {
        return respond(200, { models: ['m', 'gemini-9-flash', 'gemini-9-flash-image'].map((n) =>
          ({ name: `models/${n}`, supportedGenerationMethods: ['generateContent'] })) });
      }
      tried.push(modelOf(req));
      return tried.at(-1) === 'm' ? respond(503, 'high demand') : reply(GOOD);
    }).fromText('air force 1');
    expect(out.item.brand).toBe('Nike');
    expect(tried.at(-1)).toBe('gemini-9-flash');
    expect(tried).not.toContain('gemini-9-flash-image');
  });

  it("doesn't try other models with a bad key", async () => {
    const tried = [];
    await fails(make((req) => { tried.push(modelOf(req)); return respond(403, 'API key not valid'); },
      { env: { GEMINI_FALLBACK_MODELS: 'other' } }).fromText('x'));
    expect(new Set(tried)).toEqual(new Set(['m']));
  });

  it('gives up inside the time budget', async () => {
    let t = 0;
    const ident = new Identifier({ env: { GEMINI_API_KEY: 'k', GEMINI_FALLBACK_MODELS: '' }, model: 'm', budgetS: 70,
      now: () => t, sleep: async (s) => { t += s; }, transport: async () => { t += 30; return respond(503, 'high demand'); } });
    await fails(ident.fromText('x'));
    expect(t).toBeLessThanOrEqual(75);
  });

  it('lets Claude step in when Gemini is busy', async () => {
    const order = [];
    const out = await make((req) => {
      if (modelOf(req) === 'claude') {
        order.push('claude');
        const body = JSON.parse(req.body);
        expect(body.messages[0].content.map((b) => b.type)).toEqual(['image', 'text']);
        expect(body.model).toBe('claude-haiku-5-5');
        expect(req.headers['x-api-key']).toBe('test-key');
        return respond(200, { type: 'message', content: [{ type: 'text', text: 'Here it is: ' + JSON.stringify(GOOD) }] });
      }
      order.push(modelOf(req));
      return respond(503, 'high demand');
    }, { env: { ANTHROPIC_API_KEY: 'test-key', GEMINI_FALLBACK_MODELS: 'gemini-other-flash' } }).fromImages([JPEG]);
    expect(out.item.brand).toBe('Nike');
    expect(out.model).toBe('claude:claude-haiku-5-5');
    expect(out.fallback).toBe(true);
    expect(order[0]).toBe('m');
    expect(order.at(-1)).toBe('claude');
    expect(order).not.toContain('gemini-other-flash');
  });

  it("moves on from a slow Gemini instead of waiting again", async () => {
    const tried = [];
    const out = await make((req) => {
      tried.push(modelOf(req));
      if (tried.at(-1) === 'm') throw timeoutError();
      return reply(GOOD);
    }, { env: { GEMINI_FALLBACK_MODELS: 'gemini-other-flash' } }).fromText('air force 1');
    expect(tried).toEqual(['m', 'gemini-other-flash']);
    expect(out.item.brand).toBe('Nike');
  });

  it('asks for low thinking, and drops it if refused', async () => {
    expect(thinkingConfig('gemini-3.8-flash')).toEqual({ thinkingLevel: 'low' });
    expect(thinkingConfig('gemini-2.5-flash')).toEqual({ thinkingBudget: 0 });
    const seen = [];
    await new Identifier({ env: { GEMINI_API_KEY: 'k' }, model: 'gemini-3.8-flash', sleep: async () => {},
      transport: async (req) => {
        const cfg = JSON.parse(req.body).generationConfig;
        seen.push('thinkingConfig' in cfg);
        return 'thinkingConfig' in cfg ? respond(400, 'Unknown field thinkingConfig') : reply(GOOD);
      } }).fromText('x');
    expect(seen).toEqual([true, false]);
  });

  const BAD = [
    respond(200, { candidates: [{ content: { parts: [{ text: '{"title": "Nike Air' }] }, finishReason: 'MAX_TOKENS' }] }),
    respond(200, { candidates: [{ finishReason: 'SAFETY' }] }),
    respond(200, { promptFeedback: { blockReason: 'OTHER' } }),
    respond(200, { candidates: [{ content: { parts: [{ text: "I can't tell what this is." }] } }] }),
    respond(200, { candidates: [] }),
  ];
  it.each(BAD.map((r, i) => [i, r]))('moves on to the backup after unusable answer %i', async (_, bad) => {
    const order = [];
    const out = await make((req) => {
      order.push(modelOf(req));
      return order.at(-1) === 'claude' ? claudeOk() : bad.clone();
    }, { env: { ANTHROPIC_API_KEY: 'test-key', GEMINI_FALLBACK_MODELS: '' } }).fromImages([JPEG]);
    expect(order).toEqual(['m', 'claude']);
    expect(out.item.brand).toBe('Nike');
    expect(out.fallback).toBe(true);
  });

  it("doesn't let the first model use the whole budget", async () => {
    const timeouts = {};
    const out = await make((req) => {
      timeouts[modelOf(req)] = req.timeoutS;
      if (modelOf(req) === 'm') throw timeoutError();
      return claudeOk();
    }, { env: { ANTHROPIC_API_KEY: 'test-key', GEMINI_FALLBACK_MODELS: '' }, budgetS: 170 }).fromImages([JPEG]);
    expect(timeouts.m).toBeLessThanOrEqual(40);
    expect(timeouts.claude).toBeGreaterThan(40);
    expect(out.item.brand).toBe('Nike');
  });

  it('still tries the backup after other request errors', async () => {
    const order = [];
    const out = await make((req) => {
      order.push(modelOf(req));
      return order.at(-1) === 'claude' ? claudeOk() : respond(400, { error: { message: 'Unable to process input image.' } });
    }, { env: { ANTHROPIC_API_KEY: 'test-key', GEMINI_FALLBACK_MODELS: '' } }).fromImages([JPEG]);
    expect(out.item.brand).toBe('Nike');
    expect(order).toEqual(['m', 'claude']);
  });

  it('skips other Geminis after a refused key, but tries Claude', async () => {
    const order = [];
    await make((req) => {
      order.push(modelOf(req));
      return order.at(-1) === 'claude' ? claudeOk() : respond(403, 'API key not valid');
    }, { env: { ANTHROPIC_API_KEY: 'test-key', GEMINI_FALLBACK_MODELS: 'other' } }).fromText('x');
    expect(order).toEqual(['m', 'claude']);
  });

  it('says why when everything was just busy', async () => {
    let calls = 0;
    const e = await fails(make(() => { calls++; return respond(503, 'high demand'); },
      { env: { GEMINI_FALLBACK_MODELS: '' }, budgetS: 170 }).fromImages([JPEG]));
    expect(calls).toBe(4);  // one model and its retries
    expect(e.reason()).toContain('m: busy (HTTP 503)');
    expect(e.reason()).toContain('ANTHROPIC_API_KEY');
  });

  it('works with Claude alone, without a Gemini key', async () => {
    const seen = [];
    const out = await make((req) => { seen.push(modelOf(req)); return claudeOk(); },
      { key: '', env: { ANTHROPIC_API_KEY: 'test-key' } }).fromImages([JPEG]);
    expect(out.item.brand).toBe('Nike');
    expect(seen).toEqual(['claude']);
  });

  it('can put Claude first for photos', async () => {
    const order = [];
    await make((req) => { order.push(modelOf(req)); return claudeOk(); },
      { env: { ANTHROPIC_API_KEY: 'test-key', PHOTO_AI_FIRST: 'claude' } }).fromImages([JPEG]);
    expect(order).toEqual(['claude']);
  });

  it("doesn't ask Claude to guess without the photo (HEIC)", async () => {
    const order = [];
    const out = await make((req) => { order.push(modelOf(req)); return reply(GOOD); },
      { env: { ANTHROPIC_API_KEY: 'test-key', PHOTO_AI_FIRST: 'claude' } }).fromImages([HEIC]);
    expect(order).toEqual(['m']);
    expect(out.item.brand).toBe('Nike');
  });
});

describe('usage limits', () => {
  it('calls a quota 429 a usage limit, not busy', async () => {
    const msg = { error: { code: 429, status: 'RESOURCE_EXHAUSTED',
      message: 'You exceeded your current quota. Quota exceeded for metric: generate_content_free_tier_requests' } };
    const e = await fails(make(() => respond(429, msg), { env: { GEMINI_FALLBACK_MODELS: '' }, budgetS: 170 }).fromImages([JPEG]));
    expect(e.reason()).toContain('usage limit reached (HTTP 429: You exceeded your current quota');
    expect(e.reason()).not.toContain('busy');
  });

  it("doesn't retry a 429 on the same model", async () => {
    const order = [];
    const out = await make((req) => {
      order.push(modelOf(req));
      return order.at(-1) === 'm' ? respond(429, { error: { message: 'quota' } }) : reply(GOOD);
    }, { env: { GEMINI_FALLBACK_MODELS: 'other' } }).fromText('x');
    expect(out.item.brand).toBe('Nike');
    expect(order).toEqual(['m', 'other']);
  });

  const quota429 = (qid = 'GenerateRequestsPerMinutePerProjectPerModel-FreeTier', delay = '37s') => respond(429, { error: {
    code: 429, status: 'RESOURCE_EXHAUSTED',
    message: 'You exceeded your current quota, please check your plan and billing details. For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits.',
    details: [{ '@type': 'type.googleapis.com/google.rpc.QuotaFailure',
      violations: [{ quotaMetric: 'generativelanguage.googleapis.com/generate_content_free_tier_requests', quotaId: qid, quotaValue: '20' }] },
    { '@type': 'type.googleapis.com/google.rpc.RetryInfo', retryDelay: delay }] } });

  it('skips a model out of allowance on the next identify', async () => {
    const calls = [];
    const cooldowns = new MemoryCooldowns();
    const handler = (req) => { calls.push(modelOf(req)); return calls.at(-1) === 'm' ? quota429() : reply(GOOD); };
    const opts = { env: { GEMINI_FALLBACK_MODELS: '', GEMMA_MODEL: 'gemma-4-31b-it' }, cooldowns };
    await make(handler, opts).fromText('x');
    await make(handler, opts).fromText('x');
    expect(calls).toEqual(['m', 'gemma-4-31b-it', 'gemma-4-31b-it']);
  });

  it('brings a skipped model back after its limit resets', async () => {
    let t = 1_000_000;
    const cooldowns = new MemoryCooldowns();
    const calls = [];
    const handler = (req) => { calls.push(modelOf(req)); return calls.length === 1 ? quota429('PerMinute', '30s') : reply(GOOD); };
    const opts = { env: { GEMINI_FALLBACK_MODELS: '', GEMMA_MODEL: 'gemma-4-31b-it' }, cooldowns, now: () => t };
    await make(handler, opts).fromText('x');
    t += 31;
    await make(handler, opts).fromText('x');
    expect(calls).toEqual(['m', 'gemma-4-31b-it', 'm']);
    expect((await cooldowns.load()).has('m')).toBe(false);
  });

  it('waits for a daily limit until midnight Pacific time', () => {
    const t = 1_761_400_000;  // 25 Oct 2025, mid-afternoon in London
    const until = nextPacificMidnight(t);
    const pt = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Los_Angeles', hourCycle: 'h23', hour: 'numeric', minute: 'numeric' })
      .format(new Date(until * 1000));
    expect(until).toBeGreaterThan(t);
    expect(pt).toBe('00:00');
    expect(until - t).toBeLessThan(26 * 3600);
    // and across the clocks going back (2 Nov 2025 in the US)
    const dst = nextPacificMidnight(Date.UTC(2025, 10, 2, 12) / 1000);
    expect(new Date(dst * 1000).toISOString()).toBe('2025-11-03T08:00:05.000Z');
  });

  it("keeps Google's full message and says when it tries again", async () => {
    const cooldowns = new MemoryCooldowns();
    const opts = { env: { GEMINI_FALLBACK_MODELS: '', GEMMA_MODEL: '' }, cooldowns };
    const e = await fails(make(() => quota429(), opts).fromText('x'));
    expect(e.reason()).toContain('head to: https://ai.google.dev/gemini-api/docs/rate-limits.');
    expect(e.reason()).toContain('per-minute limit: GenerateRequestsPerMinutePerProjectPerModel-FreeTier (limit 20)');
    expect(e.reason()).toContain('skipped until');
    const e2 = await fails(make(() => quota429(), opts).fromText('x'));
    expect(e2.reason()).toContain('m: skipped: usage limit reached at');
  });

  it('keeps cooldowns in D1 for the next request', async () => {
    const cooldowns = new D1Cooldowns(env.DB);
    await fails(make(() => quota429('GenerateRequestsPerDayPerProjectPerModel-FreeTier'),
      { env: { GEMINI_FALLBACK_MODELS: '', GEMMA_MODEL: '' }, cooldowns, now: () => Date.now() / 1000 }).fromText('x'));
    const row = await env.DB.prepare("SELECT why FROM model_cooldowns WHERE model = 'm'").first();
    expect(row.why).toContain('daily limit');
    const e = await fails(make(() => reply(GOOD), { env: { GEMINI_FALLBACK_MODELS: '', GEMMA_MODEL: '' },
      cooldowns: new D1Cooldowns(env.DB), now: () => Date.now() / 1000 }).fromText('x'));
    expect(e.reason()).toContain('m: skipped');
  });
});

describe('Gemma', () => {
  const MODELS = { models: ['m', 'gemini-9-flash', 'gemma-3-27b-it', 'gemma-4-26b-a4b-it', 'gemma-4-31b-it', 'gemma-4-31b-it-image']
    .map((n) => ({ name: `models/${n}`, supportedGenerationMethods: ['generateContent'] })) };

  it('is found and tried after Flash, with the models list fetched once', async () => {
    const order = [];
    let gets = 0;
    const out = await make((req) => {
      if (req.method === 'GET') { gets++; return respond(200, MODELS); }
      order.push(modelOf(req));
      return order.at(-1).startsWith('gemma') ? reply(GOOD) : respond(429, { error: { message: 'quota' } });
    }).fromImages([JPEG]);
    expect(order).toEqual(['m', 'gemini-9-flash', 'gemma-4-31b-it']);
    expect(out.model).toBe('gemma-4-31b-it');
    expect(gets).toBe(1);
  });

  it('gets JSON asked for in the prompt', async () => {
    const seen = {};
    const out = await make((req) => {
      const body = JSON.parse(req.body);
      Object.assign(seen, { model: modelOf(req), cfg: body.generationConfig, text: body.contents[0].parts[0].text,
        image: 'inline_data' in body.contents[0].parts[1] });
      return textReply('```json\n' + JSON.stringify(GOOD) + '\n```');
    }, { env: { GEMINI_FALLBACK_MODELS: '', GEMMA_MODEL: 'gemma-4-31b-it', PHOTO_AI_FIRST: 'gemma' } }).fromImages([JPEG]);
    expect(seen.model).toBe('gemma-4-31b-it');
    expect(seen.image).toBe(true);
    expect(out.item.brand).toBe('Nike');
    expect(seen.cfg).not.toHaveProperty('responseMimeType');
    expect(seen.cfg).not.toHaveProperty('responseSchema');
    expect(seen.cfg).not.toHaveProperty('thinkingConfig');
    expect(seen.text).toContain('Reply with only one JSON object');
    expect(out.fallback).toBe(false);  // asked first on purpose (PHOTO_AI_FIRST): not "the main model didn't answer"
  });

  it('asks a model that refuses JSON mode in the prompt instead', async () => {
    const cfgs = [];
    const out = await make((req) => {
      const cfg = JSON.parse(req.body).generationConfig;
      cfgs.push('responseMimeType' in cfg);
      return 'responseMimeType' in cfg ? respond(400, { error: { message: 'JSON mode is not enabled for models/m' } }) : reply(GOOD);
    }, { env: { GEMINI_FALLBACK_MODELS: '' } }).fromText('x');
    expect(out.item.brand).toBe('Nike');
    expect(cfgs).toEqual([true, false]);
  });

  it('counts as a backup in the message', async () => {
    const e = await fails(make(() => respond(429, { error: { message: 'quota' } }),
      { env: { GEMINI_FALLBACK_MODELS: '', GEMMA_MODEL: 'gemma-4-31b-it' } }).fromText('x'));
    expect(e.reason()).toContain('gemma-4-31b-it: usage limit reached');
    expect(e.reason()).not.toContain('no backup AI');
  });

  it('gets its long timeout even when first', async () => {
    const seen = {};
    await make((req) => { seen[modelOf(req)] = req.timeoutS; return reply(GOOD); },
      { env: { GEMINI_FALLBACK_MODELS: '', GEMMA_MODEL: 'gemma-4-31b-it', PHOTO_AI_FIRST: 'gemma' }, budgetS: 180 }).fromImages([JPEG]);
    expect(seen['gemma-4-31b-it']).toBeGreaterThanOrEqual(150);
  });

  it('still fits three minutes when slow after a skipped Flash', async () => {
    let t = 0;
    const e = await fails(make((req) => {
      if (modelOf(req) === 'm') return quota429();
      t += req.timeoutS;  // Gemma uses its whole wait
      throw timeoutError();
    }, { env: { GEMINI_FALLBACK_MODELS: '', GEMMA_MODEL: 'gemma-4-31b-it' }, budgetS: 180, now: () => t }).fromImages([JPEG]));
    expect(t).toBeLessThanOrEqual(180);
    expect(e.reason()).toContain('gemma-4-31b-it: no answer within 160 s');
  });

  it('tries every usable model, best first', async () => {
    const names = ['m', 'gemini-3-pro-preview', 'gemini-2.5-flash', 'gemini-3.5-flash-lite', 'gemini-3.5-flash',
      'gemini-2.5-flash-preview-tts', 'gemini-3-flash-image', 'text-embedding-004', 'gemini-3-flash-live',
      'gemma-3-1b-it', 'gemma-3-27b-it', 'gemma-4-31b-it', 'gemma-4-26b-a4b-it', 'aqa'];
    const order = [];
    await fails(make((req) => {
      if (req.method === 'GET') return respond(200, { models: names.map((n) => ({ name: `models/${n}`, supportedGenerationMethods: ['generateContent'] })) });
      order.push(modelOf(req));
      return respond(429, { error: { message: 'quota' } });
    }, { budgetS: 180 }).fromText('x'));
    expect(order).toEqual(['m', 'gemini-3.5-flash', 'gemini-2.5-flash', 'gemini-3.5-flash-lite', 'gemini-3-pro-preview',
      'gemma-4-31b-it', 'gemma-4-26b-a4b-it', 'gemma-3-27b-it']);
  });
});

describe('reading answers', () => {
  const WRAPPED = [
    'Sure! Looking at the photo, this is a pair of trainers.\n\n```json\n' + JSON.stringify(GOOD, null, 2) + '\n```\n\nLet me know {if} you need more.',
    'Notes: brand looks like {Nike}.\nAnswer:\n' + JSON.stringify(GOOD),
    JSON.stringify(GOOD, null, 2).slice(0, -2).trimEnd() + ',\n  // sure about this\n}',   // trailing comma + comment
    pyRepr(GOOD),                                                                         // Python-style dict
    '```json\n' + pyDumps(GOOD).slice(0, -40),                                            // cut off part-way
  ];
  it.each(WRAPPED.map((t, i) => [i, t]))('finds the JSON however answer %i wraps it', async (_, text) => {
    const out = await make(() => textReply(text)).fromText('x');
    expect(out.item.title.startsWith('Nike Air Force 1')).toBe(true);
  });

  it('ignores thinking parts', async () => {
    const parts = [{ text: '{not json, just thinking about the {photo}', thought: true }, { text: JSON.stringify(GOOD) }];
    expect((await make(() => textReply('', parts)).fromText('x')).item.brand).toBe('Nike');
  });

  it('shows an unreadable answer in the warning', async () => {
    const e = await fails(make(() => textReply('I think this is a Nike trainer, maybe an Air Force 1.'),
      { env: { GEMINI_FALLBACK_MODELS: '' } }).fromText('x'));
    expect(e.reason()).toContain("answer wasn't valid JSON (it said: «I think this is a Nike trainer, maybe an Air Force 1.»)");
  });

  it("isn't confused by escaped quotes and braces inside strings", async () => {
    const obj = { ...GOOD, title: 'Nike Air Force 1 "07 {Low}" White' };
    const text = 'Here {you} go:\n' + JSON.stringify(obj) + '\nHope that helps}';
    expect((await make(() => textReply(text)).fromText('x')).item.title).toBe('Nike Air Force 1 "07 {Low}" White');
    expect(findJson("{'title': 'It\\'s a \"test\"', 'is_broken': False}")).toEqual({ title: 'It\'s a "test"', is_broken: false });
  });
});

describe('routes', () => {
  const call = (path, init) => exports.default.fetch(new Request('https://api.test' + path, init));

  it('identify-text without a Gemini key returns the description as the search, with a warning', async () => {
    const r = await call('/api/v1/identify-text', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query: 'Sony WH-1000XM4 Black' }) });
    expect(r.status).toBe(200);
    const body = await r.json();
    expect(body).toMatchObject({ schema_version: '1.1', source: 'text', cleaned_image_url: null });
    expect(body.item.search_queries.ebay).toBe('sony wh-1000xm4 black');
    expect(body.warnings[0]).toContain('GEMINI_API_KEY is not set');
  });

  it('identify without a working AI returns a blank form and says why', async () => {
    const form = new FormData();
    form.append('images', new File([JPEG], 'a.jpg', { type: 'image/jpeg' }));
    const r = await call('/api/v1/identify', { method: 'POST', body: form });
    expect(r.status).toBe(200);
    const body = await r.json();
    expect(body).toMatchObject({ source: 'vision', confidence: 0, item: { title: '', condition_tier: 'good' } });
    expect(body.warnings[0]).toContain("Couldn't identify the photo automatically (GEMINI_API_KEY is not set)");
  });

  it('rejects bad requests the way FastAPI does', async () => {
    const short = await call('/api/v1/identify-text', { method: 'POST', body: JSON.stringify({ query: 'x' }) });
    expect(short.status).toBe(422);
    const tier = await call('/api/v1/identify-text', { method: 'POST', body: JSON.stringify({ query: 'xy', condition_tier: 'shiny' }) });
    expect(tier.status).toBe(422);
    const noImages = await call('/api/v1/identify', { method: 'POST', body: new FormData() });
    expect(noImages.status).toBe(422);
  });

  it('has no job routes, so the page uses the one-shot ones', async () => {
    expect((await call('/api/v1/identify/start', { method: 'POST', body: new FormData() })).status).toBe(404);
  });
});
