// FlipCurve API on Cloudflare: takes over from the FastAPI server on the laptop
// (NO_LAPTOP_PLAN.md). Same routes and JSON as backend/main.py, so neither the page nor the
// helper extension has to change. Routes arrive step by step; anything not ported yet is a 404.

import { D1Cooldowns, Identifier, IdentifyError, prettyModel } from './identify.js';

const SCHEMA_VERSION = '1.1';  // backend/models/schemas.py
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

const ROUTES = {
  'GET /api/v1/health': health,
  'POST /api/v1/identify': identifyPhotos,
  'POST /api/v1/identify-text': identifyText,
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
