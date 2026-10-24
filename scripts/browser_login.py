"""One-time sign-in for the app's browser (the same profile the fetcher uses).

eBay only shows sold/completed listings to signed-in visitors (since July 2026), so the
fetcher's browser needs a signed-in session. This opens that browser WITH a window on eBay's
sign-in page; you sign in yourself (use the dedicated FlipCurve account, not your main one),
then press Enter here. The session is kept in backend/db/browser_profile/ (git-ignored).

    python scripts/browser_login.py
Nothing here ever handles your password: you type it into eBay's own page.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PROFILE = ROOT / "backend" / "db" / "browser_profile"
SIGN_IN = "https://signin.ebay.co.uk/"
CHECK = "https://www.ebay.co.uk/sch/i.html?_nkw=nike+air+force+1&LH_Sold=1&LH_Complete=1"

if __name__ == "__main__":
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(str(PROFILE), headless=False, locale="en-GB")
        page = ctx.new_page()
        page.goto(SIGN_IN)
        input("\nSign in to eBay in the browser window, then come back here and press Enter... ")
        page.goto(CHECK, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        signed_in = "signin" not in page.url
        n = page.locator("li[data-listingid]").count()
        print(f"sold-search check: {'OK' if signed_in and n else 'NOT signed in'} ({n} listings on the page)")
        ctx.close()
