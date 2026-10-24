"""Page fetching for eBay and Vinted search pages: polite and honest (DECISIONS.md D10).

kind (or env FLIPCURVE_FETCHER):
  'browser' (default) - a real Chromium via Playwright, loading pages like a person would:
                        one page at a time, a pause between pages, cookies kept in a local
                        profile. Needs: pip install playwright && python -m playwright install chromium
  'httpx'             - plain requests that say who they are in the User-Agent. Fine for
                        sites that allow it; eBay usually refuses.

Rules we keep: low volume (a few pages per demo item), a delay between requests, results
cached so we fetch once and reuse, and NO attempts to get around a refusal. If a site
answers 403 or shows a bot check, we raise Blocked and the caller uses cached data.

Owner: Person 2.
"""
from __future__ import annotations

import os
import time

HONEST_UA = "FlipCurve/0.1 (OxHack 2026 student price-guide prototype)"
BLOCK_MARKERS = ("Pardon Our Interruption", "captcha", "Checking your browser")


class Blocked(RuntimeError):
    """The site refused the request (403 / bot check). Use cached data; don't retry around it."""


class Fetcher:
    def __init__(self, delay_s: float = 1.5, timeout_s: float = 20.0, kind: str | None = None):
        self.delay_s, self.timeout_s, self._last = delay_s, timeout_s, 0.0
        kind = kind or os.getenv("FLIPCURVE_FETCHER") or "browser"
        if kind == "browser":
            try:
                from playwright.sync_api import sync_playwright  # type: ignore
            except ImportError:
                kind = "httpx"  # Playwright not installed: fall back to plain, honest requests
            else:
                self._pw = sync_playwright().start()
                profile = os.path.join(os.path.dirname(__file__), "..", "db", "browser_profile")
                self._ctx = self._pw.chromium.launch_persistent_context(
                    profile, headless=os.getenv("FLIPCURVE_HEADLESS", "1") == "1", locale="en-GB")
                self._page = self._ctx.new_page()
        if kind == "httpx":
            import httpx
            self._session = httpx.Client(headers={"User-Agent": HONEST_UA, "Accept-Language": "en-GB,en;q=0.9"},
                                         follow_redirects=True, timeout=timeout_s)
        self.kind = kind

    def get(self, url: str) -> str:
        wait = self.delay_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            if self.kind == "browser":
                resp = self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_s * 1000)
                status, text = (resp.status if resp else 0), self._page.content()
            else:
                r = self._session.get(url)
                status, text = r.status_code, r.text
        finally:
            self._last = time.monotonic()
        if status == 404:
            raise Blocked(f"HTTP 404 from {url}")  # callers treat 404 as 'gone'
        if status != 200 or any(m.lower() in text[:5000].lower() for m in BLOCK_MARKERS):
            raise Blocked(f"HTTP {status} from {url}")
        return text

    def close(self):
        if self.kind == "browser":
            self._ctx.close()
            self._pw.stop()
        else:
            self._session.close()
