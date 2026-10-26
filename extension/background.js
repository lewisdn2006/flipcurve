// FlipCurve helper: loads a handful of eBay and Vinted pages in the user's own browser when the
// FlipCurve website asks (only lewisdn.com / localhost can talk to it - see manifest.json).
//
// Rules (DECISIONS.md D10, D14): scans run only when the user presses Scan; one page at a time
// per site with a pause (longer for Vinted); nothing disguised. On a sign-in or bot-check page we STOP that site and report it;
// we never try to get around it. The one thing we do fix is an expired Vinted session, the way
// visiting the site would: see renewVintedSession.
// Background re-checks (D14, can be switched off in the popup): every 2 hours while Chrome is
// open, re-open up to 20 Vinted listings seen in earlier scans, so the server learns which sold
// (the Cloudflare server gets just each listing's status, worked out here: classifyItemPage).

const PAUSE_MS = 1500;
const EBAY = "https://www.ebay.co.uk/sch/i.html";
const VINTED = "https://www.vinted.co.uk";
const SIGNIN = /\/\/signin\.ebay\.|\/signin\//i;
const CHALLENGE = /splashui\/challenge|\/captcha|\/challenge/i;
const SESSION_REFRESH = /\/session-refresh(\?|$)/i;
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

// Pacing per site. Vinted slows right down after ~14 requests in a minute (25 Oct: two scans
// both died on the 15th Vinted request; a stalled request makes Chrome stop the helper), so Vinted
// gets a longer gap. eBay and Vinted run side by side, so the gap costs little.
const GAP_MS = { "www.vinted.co.uk": 4500, "www.ebay.co.uk": 1500 };
const TIMEOUT_MS = 20000;   // give up on a request that stalls (Chrome stops a worker on a 30 s stall)
const lastHit = {};

async function load(url, gapMs) {
  const host = new URL(url).host;
  const gap = gapMs ?? GAP_MS[host] ?? 1500;
  const wait = gap - (Date.now() - (lastHit[host] || 0));
  lastHit[host] = Date.now() + Math.max(wait, 0);   // reserve the slot before waiting
  if (wait > 0) await sleep(wait);
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), TIMEOUT_MS);
  try {
    const r = await fetch(url, { credentials: "include", redirect: "follow", signal: ctrl.signal });
    const html = await r.text();
    const finalUrl = r.url || url;
    const title = titleOf(html).toLowerCase();
    let stop = null;
    if (SIGNIN.test(finalUrl)) stop = "sign_in_required";
    else if (SESSION_REFRESH.test(finalUrl) || title === "session refresh") stop = "session_expired";
    else if (CHALLENGE.test(finalUrl) || BLOCK_TITLES.some((t) => title.includes(t)) || r.status === 403 || r.status === 429) stop = "refused";
    return { status: r.status, finalUrl, html, stop };
  } catch (e) {
    const slow = e && e.name === "AbortError";
    return { status: 0, finalUrl: url, html: "", stop: slow ? "slow" : "network_error", error: String(e) };
  } finally {
    clearTimeout(timer);
    lastHit[host] = Date.now();
  }
}

// Vinted's session cookie runs out now and then. Every page then redirects to /session-refresh,
// a placeholder whose script renews the cookie, which fetch() never runs (26 Oct: three scans
// got no Vinted listings this way). So open Vinted in a background tab for a moment, as visiting
// the site would, and close it again. Only during a scan someone started; anything else Vinted
// shows in that tab (a check, a sign-in) is left alone, and the retry then stops as usual.
const RENEW_WAIT_MS = 20000;
let renewing = null;

function renewVintedSession() {
  renewing ??= (async () => {
    let tab = null;
    try {
      tab = await chrome.tabs.create({ url: `${VINTED}/`, active: false });
      const until = Date.now() + RENEW_WAIT_MS;
      while (Date.now() < until) {
        await sleep(500);
        const t = await chrome.tabs.get(tab.id);
        const url = t.url || t.pendingUrl || "";
        if (t.status === "complete" && !SESSION_REFRESH.test(url)) {
          await sleep(1500);   // let the page's own requests finish setting the cookie
          return true;
        }
      }
      return false;
    } catch {
      return false;   // e.g. no Chrome window open
    } finally {
      if (tab) chrome.tabs.remove(tab.id).catch(() => {});
      renewing = null;
    }
  })();
  return renewing;
}

// Keep the worker awake during long runs: extension API calls reset Chrome's idle timer.
function keepAwake() {
  const t = setInterval(() => chrome.runtime.getPlatformInfo(() => void chrome.runtime.lastError), 15000);
  return () => clearInterval(t);
}

// What the last scan did, for the popup and for the site when a scan ends early.
async function logScan(update) {
  const { lastScan = {} } = await chrome.storage.local.get("lastScan");
  await chrome.storage.local.set({ lastScan: { ...lastScan, ...update, updatedAt: Date.now() } });
}
self.addEventListener("error", (e) => { logScan({ crash: String(e.message || e) }); });
self.addEventListener("unhandledrejection", (e) => { logScan({ crash: String(e.reason) }); });

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
  // 8 Vinted catalogue pages (~96 each; stops early) and 4 Vinted item pages to date them.
  const clamp = (x, lo, hi) => Math.max(lo, Math.min(hi, Number.isFinite(Number(x)) ? Number(x) : lo));
  const ebayPages = clamp(req.ebayPages ?? 4, 0, 6), vintedPages = clamp(req.vintedPages ?? 8, 0, 10);
  const vintedItemSamples = clamp(req.vintedItemSamples ?? 4, 0, 8);
  const { queries = {} } = req;
  const recheckVintedIds = (req.recheckVintedIds || []).slice(0, 6);
  // Each page goes to the site as soon as it's loaded (one small message each).
  let sent = 0;
  const stopped = {};
  const steps = (queries.ebay ? 2 * ebayPages : 0) + (queries.vinted ? vintedPages + vintedItemSamples + recheckVintedIds.length : 0);
  let done = 0;
  const step = (label) => { progress({ type: "progress", done: ++done, total: steps, label }); logScan({ step: done, steps, label }); };
  const keep = (platform, kind, page, url, res, html) => {
    sent++;
    progress({ type: "page", page: { platform, kind, page, url, final_url: res.finalUrl, status: res.status, html,
                                     fetched_at: new Date().toISOString() } });
  };

  // eBay: newest-listed pages (start times + calibration), then recently-sold pages
  async function ebay() {
    if (!queries.ebay || !ebayPages) return;
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

  // Vinted pages, renewing an expired session once per scan and trying the page again
  let renewed = false;
  async function loadVinted(url) {
    let res = await load(url);
    if (res.stop === "session_expired" && !renewed) {
      renewed = true;
      progress({ type: "progress", done, total: steps, label: "Renewing the Vinted session" });
      logScan({ label: "renewing the Vinted session" });
      if (await renewVintedSession()) res = await load(url);
    }
    return res;
  }

  // Vinted: newest catalogue pages, a few item pages to date listing IDs, and any re-checks
  async function vinted() {
    if (!queries.vinted) return;
    let ids = (req.vintedIds || []).map(String);
    for (let p = 1; p <= vintedPages; p++) {
      const url = vintedCatalogUrl(queries.vinted, p);
      const res = await loadVinted(url);
      step(`Vinted listings ${p}/${vintedPages}`);
      if (res.stop) { stopped.vinted = { reason: res.stop, url }; return; }
      keep("vinted", "catalog", p, url, res, trimAround(res.html, 'data-testid="grid-item"', 20000));
      const found = [...res.html.matchAll(/\/items\/(\d{8,12})-/g)].map((m) => m[1]);
      ids.push(...found);
      if (new Set(found).size < 40) break;  // last page
    }
    ids = [...new Set(ids)].sort();
    const todo = [...spread(ids, vintedItemSamples).map((id) => ["item", id]),
                  ...recheckVintedIds.map((id) => ["recheck", String(id)])];
    for (const [kind, id] of todo) {
      const url = `${VINTED}/items/${id}`;
      const res = await loadVinted(url);
      step(kind === "item" ? "Vinted listing ages" : "Re-checking earlier Vinted listings");
      if (res.stop && res.status !== 404) { stopped.vinted = { reason: res.stop, url }; return; }
      keep("vinted", kind, null, url, res, slim(res.html));
    }
  }

  const stopAwake = keepAwake();
  await logScan({ startedAt: Date.now(), step: 0, steps, label: "starting", stopped: null, crash: null, finishedAt: null });
  try {
    await Promise.all([ebay(), vinted()]);   // the two sites side by side, each at its own pace
  } finally {
    stopAwake();
  }
  await logScan({ finishedAt: Date.now(), stopped, sent });
  return { type: "result", ok: true, pages: [], sent, stopped, version: chrome.runtime.getManifest().version };
}

// A listing page's status ('active', 'sold', 'deleted', 'unknown') without parsing the HTML: a copy of
// web/api-worker/src/scrapers/classify.js (keep the two in step), which matches the server's reader.
// Background re-checks send just this to the Cloudflare server, which has little CPU to spare.
const LD_JSON = /<script\b[^>]*\btype\s*=\s*["']?application\/ld\+json["']?[^>]*>([\s\S]*?)<\/script\s*>/i;
const BUY_BUTTON = /\bdata-testid\s*=\s*["']item-buy-button["']/i;
const HIDDEN = /<(script|style|template)\b[^>]*>[\s\S]*?<\/\1\s*>/gi;
const decodeEntities = (s) => s.replace(/&(amp|lt|gt|quot|#39|apos);/g, (_, e) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'", apos: "'" }[e]));

function classifyItemPage(html, httpStatus) {
  if (httpStatus === 404 || html == null) return "deleted";
  let avail = null;
  const ld = html.match(LD_JSON);
  if (ld) {
    try {
      const j = JSON.parse(decodeEntities(ld[1]));
      if (j && typeof j === "object" && !Array.isArray(j) && j.offers && typeof j.offers === "object") avail = j.offers.availability ?? null;
    } catch { /* not JSON */ }
  }
  if (avail === "InStock" && BUY_BUTTON.test(html)) return "active";
  if (avail === null && /\bSold\b/.test(decodeEntities(html.replace(HIDDEN, " ").replace(/<[^>]*>/g, " ")))) return "sold";
  return "unknown";
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
  const stopAwake = keepAwake();
  const record = async (summary) => {
    const last = { at: Date.now(), ...summary };
    await chrome.storage.local.set({ lastRecheck: last });
    return last;
  };
  try {
    const r = await fetch(`${s.api}/api/v1/recheck-due?limit=${RECHECK_MAX}`);
    if (!r.ok) throw new Error(`server answered ${r.status}`);
    // the Cloudflare server takes just each listing's status; the laptop's server reads the pages itself
    const statusesOnly = await fetch(`${s.api}/api/v1/health`).then((h) => h.json()).then((h) => h.runtime === "worker").catch(() => false);
    const due = ((await r.json()).items || []).slice(0, RECHECK_MAX);
    const results = [];
    let stopped = null;
    for (const { id, query } of due) {
      const url = `${VINTED}/items/${id}`;
      // no one is waiting: go gently. An expired session stops the run without opening a tab
      // (nothing is sent, so the listings stay due); the next scan renews it.
      const res = await load(url, 6000);
      if (res.stop && res.status !== 404) { stopped = res.stop; break; }
      const fetchedAt = new Date().toISOString();
      results.push(statusesOnly
        ? { query, id, status: classifyItemPage(res.html, res.status), fetched_at: fetchedAt }
        : { query, page: { platform: "vinted", kind: "recheck", page: null, url, final_url: res.finalUrl,
                           status: res.status, html: slim(res.html), fetched_at: fetchedAt } });
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
      if (stopped === "refused") await chrome.storage.local.set({ recheckPausedUntil: Date.now() + REFUSED_PAUSE_MS });
    }
    return await record(summary);
  } catch (e) {
    return await record({ error: String(e.message || e) });
  } finally {
    stopAwake();
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
    Promise.all([recheckSettings(), chrome.storage.local.get(["recheckProgress", "lastScan"]), chrome.alarms.get(RECHECK_ALARM)])
      .then(([st, p, alarm]) => sendResponse({ ...st, running: recheckRunning, progress: p.recheckProgress || null,
                                               lastScan: p.lastScan || null, next: alarm ? alarm.scheduledTime : null }));
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
    chrome.storage.local.get(["ebayState", "ebayCheckedAt", "lastScan"]).then((s) =>
      sendResponse({ ok: true, version: chrome.runtime.getManifest().version, ebayState: s.ebayState || "unknown",
                     ebayCheckedAt: s.ebayCheckedAt || null, lastScan: s.lastScan || null }));
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
