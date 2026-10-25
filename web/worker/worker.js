// Deployed as the "flipcurve-page" Worker on routes lewisdn.com/flipcurve* and www.lewisdn.com/flipcurve*
// (set up in the Cloudflare dashboard, 25 Oct). It serves web/flipcurve/index.html straight from
// GitHub, so pushing to main updates the live page within about a minute; no redeploy needed.
// Any other /flipcurve... path falls through to the normal lewisdn.com site.
const SRC = "https://raw.githubusercontent.com/lewisdn2006/flipcurve/main/web/flipcurve/index.html";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/flipcurve") return Response.redirect(url.origin + "/flipcurve/" + url.search, 301);
    if (url.pathname !== "/flipcurve/" && url.pathname !== "/flipcurve/index.html") return fetch(request);
    const r = await fetch(SRC, { cf: { cacheTtl: 60, cacheEverything: true } });
    if (!r.ok) return new Response("FlipCurve page unavailable", { status: 502 });
    return new Response(r.body, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-cache" } });
  },
};
