// FlipCurve helper: loads a handful of eBay and Vinted pages in the user's own browser when the
// FlipCurve website asks (only lewisdn.com / localhost can talk to it - see manifest.json).
//
// Rules (DECISIONS.md D10, D14): scans run only when the user presses Scan; one page at a time
// with a pause; nothing disguised. On a sign-in or bot-check page we STOP that site and report it;
// we never try to get around it.
// Background re-checks (D14, can be switched off in the popup): every 2 hours while Chrome is
// open, re-open up to 20 Vinted listings seen in earlier scans, so the server learns which sold.

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

// Drop what the parsers never read (scripts other than ld+json, styles, icons, comments).
function slim(html) {
  return html.replace(/<script(?![^>]*application\/ld\+json)[^>]*>[\s\S]*?<\/script>/gi, "")
    .replace(/<style[^>]*>[\s\S]*?<\/style>/gi, "").replace(/<svg[\s\S]*?<\/svg>/gi, "")
    .replace(/<!--[\s\S]*?-->/g, "");
}

// Search pages are several MB; keep the title plus the stretch that holds the listings.
function trimAround(html, marker, tail) {
  const first = html.indexOf(marker);
  if (first < 0) return slim(html.slice(0, 200000));
  const last = html.lastIndexOf(marker);
  return `<title>${titleOf(html)}</title>` + slim(html.slice(Math.max(0, first - 500), last + tail));
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
  // Defaults: up to 4 pages of each eBay search (240 a page; stops early on the last page),
  // 8 Vinted catalogue pages (~96 each; stops early) and 8 Vinted item pages to date them.
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, Number(x) || lo));
  const ebayPages = clamp(req.ebayPages ?? 4, 1, 6), vintedPages = clamp(req.vintedPages ?? 8, 1, 10);
  const vintedItemSamples = clamp(req.vintedItemSamples ?? 8, 0, 12);
  const { queries = {} } = req;
  const recheckVintedIds = (req.recheckVintedIds || []).slice(0, 10);
  // Each page goes to the site as soon as it's loaded (one small message each). Sending them all
  // at the end as one big message dropped the connection after the last page (25 Oct).
  let sent = 0;
  const stopped = {};
  const steps = 2 * ebayPages + vintedPages + vintedItemSamples + recheckVintedIds.length;
  let done = 0;
  const step = (label) => progress({ type: "progress", done: ++done, total: steps, label });
  const keep = (platform, kind, page, url, res, html) => {
    sent++;
    progress({ type: "page", page: { platform, kind, page, url, final_url: res.finalUrl, status: res.status, html,
                                     fetched_at: new Date().toISOString() } });
  };

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
      const found = [...res.html.matchAll(/\/items\/(\d{8,12})-/g)].map((m) => m[1]);
      ids.push(...found);
      if (new Set(found).size < 40) break;  // last page
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
      keep("vinted", kind, null, url, res, slim(res.html));
    }
  }
  return { type: "result", ok: true, pages: [], sent, stopped, version: chrome.runtime.getManifest().version };
}

// ------------------------------------------------------------------ background Vinted re-checks
const RECHECK_ALARM = "flipcurve-recheck";
const RECHECK_EVERY_MIN = 120;
const RECHECK_MAX = 20;
const REFUSED_PAUSE_MS = 12 * 3600 * 1000;   // after a refusal, leave Vinted alone for 12 hours
const DEFAULT_API = "https://api.lewisdn.com";
const API_OK = /^(https:\/\/([a-z0-9-]+\.)*lewisdn\.com|https:\/\/[a-z0-9-]+\.trycloudflare\.com|http:\/\/(localhost|127\.0\.0\.1)(:\d+)?)$/;
let recheckRunning = false;

async function recheckSettings() {
  const s = await chrome.storage.local.get(["recheckEnabled", "apiBase", "lastRecheck", "recheckPausedUntil"]);
  return { enabled: s.recheckEnabled !== false, api: s.apiBase || DEFAULT_API, last: s.lastRecheck || null,
           pausedUntil: s.recheckPausedUntil || 0 };
}

async function gzipJson(obj) {
  const stream = new Blob([JSON.stringify(obj)]).stream().pipeThrough(new CompressionStream("gzip"));
  return new Response(stream).blob();
}

async function backgroundRecheck(force = false) {
  const s = await recheckSettings();
  if (!s.enabled && !force) return { skipped: "switched off" };
  if (Date.now() < s.pausedUntil && !force) return { skipped: "paused after Vinted declined" };
  if (recheckRunning) return { skipped: "already running" };
  recheckRunning = true;
  const record = async (summary) => {
    const last = { at: Date.now(), ...summary };
    await chrome.storage.local.set({ lastRecheck: last });
    return last;
  };
  try {
    const r = await fetch(`${s.api}/api/v1/recheck-due?limit=${RECHECK_MAX}`);
    if (!r.ok) throw new Error(`server answered ${r.status}`);
    const due = ((await r.json()).items || []).slice(0, RECHECK_MAX);
    const results = [];
    let stopped = null;
    for (const { id, query } of due) {
      const url = `${VINTED}/items/${id}`;
      const res = await load(url);
      if (res.stop && res.status !== 404) { stopped = res.stop; break; }
      results.push({ query, page: { platform: "vinted", kind: "recheck", page: null, url, final_url: res.finalUrl,
                                    status: res.status, html: slim(res.html), fetched_at: new Date().toISOString() } });
      await chrome.storage.local.set({ recheckProgress: { done: results.length, total: due.length } });  // keeps the worker awake
    }
    let summary = { due: due.length, checked: results.length };
    if (results.length) {
      const post = await fetch(`${s.api}/api/v1/recheck-results`, {
        method: "POST", headers: { "Content-Type": "application/json", "Content-Encoding": "gzip" },
        body: await gzipJson({ results }) });
      if (!post.ok) throw new Error(`server answered ${post.status} to the results`);
      summary = { ...summary, ...(await post.json()) };
    }
    if (stopped) {
      summary.stopped = stopped;
      await chrome.storage.local.set({ recheckPausedUntil: Date.now() + REFUSED_PAUSE_MS });
    }
    return await record(summary);
  } catch (e) {
    return await record({ error: String(e.message || e) });
  } finally {
    recheckRunning = false;
    await chrome.storage.local.remove("recheckProgress");
  }
}

function scheduleRechecks() {
  chrome.alarms.create(RECHECK_ALARM, { delayInMinutes: 5, periodInMinutes: RECHECK_EVERY_MIN });
}
chrome.runtime.onInstalled.addListener(scheduleRechecks);
chrome.runtime.onStartup.addListener(scheduleRechecks);
chrome.alarms.onAlarm.addListener((a) => { if (a.name === RECHECK_ALARM) backgroundRecheck(); });

// The popup (popup.html): status, on/off switch, "check now"
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return;
  if (msg?.type === "recheck-status") {
    Promise.all([recheckSettings(), chrome.storage.local.get(["recheckProgress"]), chrome.alarms.get(RECHECK_ALARM)])
      .then(([st, p, alarm]) => sendResponse({ ...st, running: recheckRunning, progress: p.recheckProgress || null,
                                               next: alarm ? alarm.scheduledTime : null }));
    return true;
  }
  if (msg?.type === "recheck-toggle") {
    chrome.storage.local.set({ recheckEnabled: !!msg.enabled }).then(() => sendResponse({ ok: true }));
    return true;
  }
  if (msg?.type === "recheck-now") {
    backgroundRecheck(true).then(sendResponse);
    return true;
  }
});

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
    // remember which FlipCurve server the site uses, for background re-checks
    if (typeof msg.api === "string" && API_OK.test(msg.api.replace(/\/$/, ""))) {
      await chrome.storage.local.set({ apiBase: msg.api.replace(/\/$/, "") });
    }
    try {
      port.postMessage(await collect(msg, (p) => port.postMessage(p)));
    } catch (e) {
      port.postMessage({ type: "result", ok: false, error: String(e) });
    }
  });
});
