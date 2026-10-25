// Serves the FlipCurve page at lewisdn.com/flipcurve without touching the rest of lewisdn.com.
// The page then talks to the laptop backend at api.lewisdn.com (Cloudflare Tunnel).
import page from "../flipcurve/index.html";

export default {
  async fetch(request) {
    const url = new URL(request.url);
    if (url.pathname === "/flipcurve") return Response.redirect(url.origin + "/flipcurve/" + url.search, 301);
    if (url.pathname === "/flipcurve/" || url.pathname === "/flipcurve/index.html") {
      return new Response(page, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-cache" } });
    }
    return new Response("Not found", { status: 404 });
  },
};
