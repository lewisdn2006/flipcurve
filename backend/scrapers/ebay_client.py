"""eBay UK fetcher: active + sold searches -> ListingRow dicts, with start times from calibration.

Active listings come from a newest-first search, which shows each listing's exact start time
(obs 'active', start_source 'exact'). Sold listings show only the sold DATE, so their start
comes from the item-number calibration and their time-to-sale becomes an interval
(obs 'sold_interval', start_source 'id_calibration'). Sold rows the calibration can't date
are kept as 'sold_unknown_time' (they still inform the typical-price anchors).

Owner: Person 2.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from backend.engine.platform_config import PLATFORMS
from backend.scrapers.ebay_calibration import CalibrationStore
from backend.scrapers.ebay_parse import UK, condition_tier, parse_cards

BASE = "https://www.ebay.co.uk/sch/i.html"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/130.0 Safari/537.36",
    "Accept-Language": "en-GB,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}
BLOCK_MARKERS = ("Pardon Our Interruption", "captcha", "Checking your browser")


class Blocked(RuntimeError):
    """eBay served a bot-check page instead of results."""


class Fetcher:
    """Polite GET with a delay between requests and a homepage visit first (to pick up cookies).

    kind (or env FLIPCURVE_FETCHER):
      'curl_cffi' - browser-like TLS fingerprint; we must NOT override its User-Agent, or the
                    headers contradict the fingerprint, which is a classic bot signal
      'browser'   - a real Chromium via Playwright (pip install playwright &&
                    python -m playwright install chromium); slowest, hardest to block
      'httpx'     - plain requests; fine for sites with no bot protection
    Default: curl_cffi if installed, else httpx. scripts/fetch_diag.py tells you which works.
    """

    def __init__(self, delay_s: float = 0.7, timeout_s: float = 15.0, kind: str | None = None,
                 impersonate: str | None = None):
        import os
        impersonate = impersonate or os.getenv("FLIPCURVE_IMPERSONATE", "chrome")
        self.delay_s, self.timeout_s, self._last = delay_s, timeout_s, 0.0
        self._warmed: set[str] = set()
        kind = kind or os.getenv("FLIPCURVE_FETCHER") or None
        if kind in (None, "curl_cffi"):
            try:
                from curl_cffi import requests as creq  # type: ignore
                self._session = creq.Session(impersonate=impersonate)
                self.kind = "curl_cffi"
            except ImportError:
                kind = "httpx"
        if kind == "browser":
            from playwright.sync_api import sync_playwright  # type: ignore
            self._pw = sync_playwright().start()
            profile = os.path.join(os.path.dirname(__file__), "..", "db", "browser_profile")
            self._ctx = self._pw.chromium.launch_persistent_context(
                profile, headless=os.getenv("FLIPCURVE_HEADLESS", "1") == "1", locale="en-GB")
            self._page = self._ctx.new_page()
            self.kind = "browser"
        elif kind == "httpx":
            import httpx
            self._session = httpx.Client(headers=HEADERS, follow_redirects=True, timeout=timeout_s)
            self.kind = "httpx"

    def _raw_get(self, url: str) -> tuple[int, str]:
        wait = self.delay_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            if self.kind == "browser":
                resp = self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_s * 1000)
                return (resp.status if resp else 0), self._page.content()
            if self.kind == "curl_cffi":
                r = self._session.get(url, headers={"Accept-Language": "en-GB,en;q=0.9"}, timeout=self.timeout_s)
            else:
                r = self._session.get(url)
            return r.status_code, r.text
        finally:
            self._last = time.monotonic()

    def _warm(self, url: str):
        from urllib.parse import urlsplit
        host = urlsplit(url).netloc
        if host not in self._warmed:
            self._warmed.add(host)
            self._raw_get(f"https://{host}/")

    def get(self, url: str) -> str:
        self._warm(url)
        status, text = self._raw_get(url)
        if status == 403:  # one retry after a pause; cookies from the warm-up sometimes need a moment
            time.sleep(2.0)
            status, text = self._raw_get(url)
        if status != 200 or any(m.lower() in text[:5000].lower() for m in BLOCK_MARKERS):
            raise Blocked(f"HTTP {status} from {url}")
        return text

    def close(self):
        if self.kind == "browser":
            self._ctx.close()
            self._pw.stop()


def search_url(query: str, page: int = 1, sold: bool = False, uk_only: bool = True) -> str:
    q = {"_nkw": query, "_ipg": 240, "_pgn": page}
    if sold:
        q.update({"LH_Sold": 1, "LH_Complete": 1, "_sop": 13})  # most recently ended first
    else:
        q["_sop"] = 10  # newly listed first: the only sort that shows start times
    if uk_only:
        q["LH_PrefLoc"] = 1
    return f"{BASE}?{urlencode(q)}"


def calibrate(store: CalibrationStore, fetcher: Fetcher, queries: list[str], pages=(1, 3, 6, 10),
              now: datetime | None = None, log=print) -> int:
    """Add (item number, start time) points from newest-first searches. Returns points added."""
    now = now or datetime.now(timezone.utc)
    added = 0
    for q in queries:
        for p in pages:
            try:
                cards = parse_cards(fetcher.get(search_url(q, p, uk_only=False)), now)
            except Blocked as e:
                log(f"  blocked on '{q}' p{p}: {e}")
                return added
            pts = [(c["item_id"], c["start_time"]) for c in cards if c["start_time"]]
            added += store.add(pts, source=f"cal:{q}")
            if len(cards) < 50:
                break  # ran out of results for this query
    return added


def _base_row(c: dict, now: datetime) -> dict:
    cfg = PLATFORMS["ebay"]
    ship = c["shipping_gbp"] or 0.0
    fee = round(c["price_gbp"] * cfg["buyer_fee_pct"] + cfg["buyer_fee_fixed_gbp"], 2)
    return {
        "listing_id": c["item_id"], "platform": "ebay", "title": c["title"], "url": c["url"],
        "price_gbp": c["price_gbp"], "shipping_gbp": ship, "buyer_fee_gbp": fee,
        "total_buyer_price_gbp": round(c["price_gbp"] + ship + fee, 2),
        "condition_tier": condition_tier(c["condition_raw"]), "condition_raw": c["condition_raw"],
        "observed_at": now.isoformat(), "favourite_count": None,
        "best_offer_accepted": c["best_offer_accepted"] or None,
        "is_outlier": False, "outlier_reason": None,
    }


def active_rows(cards: list[dict], now: datetime) -> list[dict]:
    rows = []
    for c in cards:
        if c["start_time"] is None or c["from_abroad"]:
            continue
        age = (now - c["start_time"]).total_seconds() / 86400
        rows.append({**_base_row(c, now), "obs_type": "active", "t_days": round(max(age, 0.0), 4),
                     "t_upper_days": None, "start_source": "exact",
                     "listed_at": c["start_time"].isoformat(), "sold_at": None})
    return rows


def sold_rows(cards: list[dict], store: CalibrationStore, now: datetime) -> list[dict]:
    rows = []
    for c in cards:
        if c["sold_date"] is None or c["from_abroad"]:
            continue
        day_start = datetime(c["sold_date"].year, c["sold_date"].month, c["sold_date"].day, tzinfo=UK)
        day_end = day_start + timedelta(days=1)
        est = store.estimate_start(c["item_id"])
        base = {**_base_row(c, now), "sold_at": day_start.isoformat()}
        if est is not None:
            lo = (day_start - est.hi).total_seconds() / 86400
            hi = (day_end - est.lo).total_seconds() / 86400
            if hi >= 0:
                rows.append({**base, "obs_type": "sold_interval", "t_days": round(max(lo, 0.0), 4),
                             "t_upper_days": round(hi, 4), "start_source": "id_calibration",
                             "listed_at": est.mid.isoformat()})
                continue
        rows.append({**base, "obs_type": "sold_unknown_time", "t_days": None, "t_upper_days": None,
                     "start_source": "unknown", "listed_at": None})
    return rows


def scan(query: str, store: CalibrationStore, fetcher: Fetcher, active_pages: int = 2, sold_pages: int = 2,
         now: datetime | None = None) -> tuple[list[dict], dict]:
    """Fetch active + sold listings for one item query. Active pages also feed the calibration."""
    now = now or datetime.now(timezone.utc)
    act_cards, sold_cards = [], []
    for p in range(1, active_pages + 1):
        cards = parse_cards(fetcher.get(search_url(query, p)), now)
        act_cards += cards
        store.add([(c["item_id"], c["start_time"]) for c in cards if c["start_time"]], source=f"item:{query}")
        if len(cards) < 200:
            break
    for p in range(1, sold_pages + 1):
        cards = parse_cards(fetcher.get(search_url(query, p, sold=True)), now)
        sold_cards += cards
        if len(cards) < 200:
            break
    a, s = active_rows(act_cards, now), sold_rows(sold_cards, store, now)
    dated = [r for r in s if r["obs_type"] == "sold_interval"]
    stats = {"active": len(a), "sold": len(s), "sold_dated": len(dated),
             "coverage": round(len(dated) / len(s), 3) if s else None, "fetcher": fetcher.kind}
    return a + s, stats
