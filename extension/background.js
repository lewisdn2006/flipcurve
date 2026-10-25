// FlipCurve helper: loads a handful of eBay and Vinted pages in the user's own browser when the
// FlipCurve website asks (only lewisdn.com / localhost can talk to it - see manifest.json).
//
// Rules (DECISIONS.md D10): only when the user presses Scan, one page at a time with a pause,
// a few pages per scan, nothing disguised. On a sign-in or bot-check page we STOP that site and
// report it; we never try to get around it.

const PAUSE_MS = 1500;
const EBAY = "https://www.ebay.co.uk/sch/i.html";
const VINTED = "https://www.vinted.co.uk";
const SIGNIN = /\/\/signin\.ebay\.|\/signin\//i;
const CHALLENGE = /splashui\/challenge|\/captcha|\/challenge/i;
const BLOCK_TITLES = ["pardon our interruption", "security measure", "captcha", "checking your browser",
                      "access denied", "just a moment"];

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function ebayUrl(query, page, sold) {
  const q = new URLSearchParams({ _nkw: query, _ipg: "240", _pgn: String(page) });
  if (sold) { q.set("LH_Sold", "1"); q.set("LH_Complete", "1"); q.set("_sop", "13"); }
  else { q.set("_sop", "10"); }  // newly listed first: the only sort that shows start times
  q.set("LH_PrefLoc", "1");
  return `${EBAY}?${q}`;
}

function vintedCatalogUrl(query, page) {
  const q = new URLSearchParams({ search_text: query, order: "newest_first", page: String(page) });
  return `${VINTED}/catalog?${q}`;
}

function titleOf(html) {
  const m = html.slice(0, 20000).match(/<title[^>]*>([\s\S]*?)<\/title>/i);
  return m ? m[1].trim() : "";
}

// Search pages are several MB; keep the title plus the stretch that holds the listings.
function trimAround(html, marker, tail) {
  const first = html.indexOf(marker);
  if (first < 0) return html.slice(0, 200000);
  const last = html.lastIndexOf(marker);
  return `<title>${titleOf(html)}</title>` + html.slice(Math.max(0, first - 500), last + tail);
}

let lastPage = 0;
async function load(url) {
  const wait = PAUSE_MS - (Date.now() - lastPage);
  if (wait > 0) await sleep(wait);
  try {
    const r = await fetch(url, { credentials: "include", redirect: "follow" });
    const html = await r.text();
    lastPage = Date.now();
    const finalUrl = r.url || url;
    const title = titleOf(html).toLowerCase();
    let stop = null;
    if (SIGNIN.test(finalUrl)) stop = "sign_in_required";
    else if (CHALLENGE.test(finalUrl) || BLOCK_TITLES.some((t) => title.includes(t)) || r.status === 403 || r.status === 429) stop = "refused";
    return { status: r.status, finalUrl, html, stop };
  } catch (e) {
    lastPage = Date.now();
    return { status: 0, finalUrl: url, html: "", stop: "network_error", error: String(e) };
  }
}

function spread(ids, n) {
  if (ids.length <= n) return ids;
  const out = [];
  for (let i = 0; i < n; i++) out.push(ids[Math.round((i * (ids.length - 1)) / (n - 1))]);
  return [...new Set(out)];
}

async function setBadge(state) {
  const look = { ok: ["", "#1D9E75"], signin: ["!", "#BA7517"], refused: ["x", "#A32D2D"] }[state] || ["", "#888780"];
  await chrome.action.setBadgeText({ text: look[0] });
  await chrome.action.setBadgeBackgroundColor({ color: look[1] });
  await chrome.storage.local.set({ ebayState: state, ebayCheckedAt: Date.now() });
}

async function collect(req, progress) {
  const { queries = {}, ebayPages = 2, vintedPages = 1, vintedItemSamples = 4, recheckVintedIds = [] } = req;
  const pages = [];
  const stopped = {};
  const steps = 2 * ebayPages + vintedPages + vintedItemSamples + recheckVintedIds.length;
  let done = 0;
  const step = (label) => progress({ type: "progress", done: ++done, total: steps, label });
  const keep = (platform, kind, page, url, res, html) =>
    pages.push({ platform, kind, page, url, final_url: res.finalUrl, status: res.status, html, fetched_at: new Date().toISOString() });

  // eBay: newest-listed pages (start times + calibration), then recently-sold pages
  if (queries.ebay) {
    outer: for (const kind of ["active", "sold"]) {
      for (let p = 1; p <= ebayPages; p++) {
        const url = ebayUrl(queries.ebay, p, kind === "sold");
        const res = await load(url);
        step(`eBay ${kind === "sold" ? "sold" : "new"} listings ${p}/${ebayPages}`);
        if (res.stop) { stopped.ebay = { reason: res.stop, url }; break outer; }
        keep("ebay", kind, p, url, res, trimAround(res.html, "data-listingid", 30000));
        if ((res.html.match(/data-listingid="\d{12}"/g) || []).length < 200) break;  // last page
      }
    }
    await setBadge(stopped.ebay ? (stopped.ebay.reason === "sign_in_required" ? "signin" : "refused") : "ok");
  }

  // Vinted: newest catalogue page(s), a few item pages to date listing IDs, and any re-checks
  if (queries.vinted) {
    let ids = [];
    for (let p = 1; p <= vintedPages; p++) {
      const url = vintedCatalogUrl(queries.vinted, p);
      const res = await load(url);
      step(`Vinted listings ${p}/${vintedPages}`);
      if (res.stop) { stopped.vinted = { reason: res.stop, url }; break; }
      keep("vinted", "catalog", p, url, res, trimAround(res.html, 'data-testid="grid-item"', 20000));
      ids.push(...[...res.html.matchAll(/\/items\/(\d{8,12})-/g)].map((m) => m[1]));
    }
    ids = [...new Set(ids)].sort();
    const todo = [...spread(ids, vintedItemSamples).map((id) => ["item", id]),
                  ...recheckVintedIds.map((id) => ["recheck", String(id)])];
    for (const [kind, id] of todo) {
      if (stopped.vinted) break;
      const url = `${VINTED}/items/${id}`;
      const res = await load(url);
      step(kind === "item" ? "Vinted listing ages" : "Re-checking earlier Vinted listings");
      if (res.stop && res.status !== 404) { stopped.vinted = { reason: res.stop, url }; break; }
      keep("vinted", kind, null, url, res, res.html);
    }
  }
  return { type: "result", ok: true, pages, stopped, version: chrome.runtime.getManifest().version };
}

// One-shot messages: {type: "ping"} -> installed + last known eBay sign-in state
chrome.runtime.onMessageExternal.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "ping") {
    chrome.storage.local.get(["ebayState", "ebayCheckedAt"]).then((s) =>
      sendResponse({ ok: true, version: chrome.runtime.getManifest().version, ebayState: s.ebayState || "unknown",
                     ebayCheckedAt: s.ebayCheckedAt || null }));
    return true;
  }
  sendResponse({ ok: false, error: "unknown message" });
});

// Scans use a port so the page can show progress: connect, post {type: "collect", ...}
chrome.runtime.onConnectExternal.addListener((port) => {
  port.onMessage.addListener(async (msg) => {
    if (!msg || msg.type !== "collect") return port.postMessage({ type: "result", ok: false, error: "unknown message" });
    try {
      port.postMessage(await collect(msg, (p) => port.postMessage(p)));
    } catch (e) {
      port.postMessage({ type: "result", ok: false, error: String(e) });
    }
  });
});
