// FlipCurve API on Cloudflare: takes over from the FastAPI server on the laptop
// (NO_LAPTOP_PLAN.md). Same routes and JSON as backend/main.py, so neither the page nor the
// helper extension has to change. Routes arrive step by step; anything not ported yet is a 404.

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

const ROUTES = {
  'GET /api/v1/health': health,
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
