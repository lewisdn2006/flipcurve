"""Parse Vinted UK HTML (no network, easy to test). Structure as of 24 Oct 2026.

Catalogue pages are server-rendered: 96 listings per page in [data-testid="grid-item"]. Each
listing link carries a summary in its title attribute:
    "Nike air force 1 size 7, Brand: Nike, Condition: Good, Size: 7, 18.00 £, 19.60 £"
(the second price includes Vinted's buyer fee, 5% + £0.70). Favourites are in
[data-testid="favourite-count-text"] (empty when 0).

Item pages: JSON-LD offers.availability is "InStock" for live items; sold items keep their page
with no availability and a "Sold" mark; deleted items return 404. The upload age is shown as
relative text in [itemprop="upload_date"], e.g. "42 min ago", "a week ago", "3 weeks ago".

When the session cookie has run out, every page redirects to /session-refresh: a placeholder
titled "Session refresh" whose script renews the cookie. It holds no listings (26 Oct 2026).

Owner: Person 2.
"""
from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup

RE_SUMMARY = re.compile(
    r"^(?P<title>.*), Brand: (?P<brand>.*?), Condition: (?P<cond>[^,]+?)(?:, Size: (?P<size>.*?))?, "
    r"(?P<price>[\d,]+\.\d{2}) £, (?P<total>[\d,]+\.\d{2}) £$"
)
RE_ITEM_ID = re.compile(r"/items/(\d{8,12})")
RE_AGE = re.compile(r"(?P<n>\d+|an?|a few)\s*(?P<unit>sec|second|min|minute|hour|day|week|month|year)s?\s+ago", re.I)
UNIT_HOURS = {"sec": 1 / 3600, "second": 1 / 3600, "min": 1 / 60, "minute": 1 / 60, "hour": 1.0,
              "day": 24.0, "week": 168.0, "month": 720.0, "year": 8760.0}

RE_SESSION_REFRESH = re.compile(r"/session-refresh(\?|$)", re.I)
RE_TITLE = re.compile(r"<title[^>]*>\s*([^<]*?)\s*</title>", re.I)


def is_session_refresh(page: dict) -> bool:
    """A collected page that is Vinted's session-refresh placeholder, not the page asked for."""
    if RE_SESSION_REFRESH.search(page.get("final_url") or ""):
        return True
    m = RE_TITLE.search((page.get("html") or "")[:20000])
    return bool(m) and m.group(1).lower() == "session refresh"


CONDITION_TIER = {"New with tags": "mint", "New without tags": "mint", "Very good": "very_good",
                  "Good": "good", "Satisfactory": "fair"}


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def parse_catalog(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for g in soup.select('[data-testid="grid-item"]'):
        a = g.select_one('a[href*="/items/"]')
        if a is None:
            continue
        m_id = RE_ITEM_ID.search(a.get("href", ""))
        m = RE_SUMMARY.match((a.get("title") or "").strip())
        if not m_id or not m or m_id.group(1) in seen:
            continue
        seen.add(m_id.group(1))
        fav = g.select_one('[data-testid="favourite-count-text"]')
        fav_txt = fav.get_text(strip=True) if fav else ""
        price, total = _num(m["price"]), _num(m["total"])
        out.append({
            "item_id": m_id.group(1), "title": m["title"], "brand": m["brand"],
            "condition_raw": m["cond"], "size": m["size"], "price_gbp": price,
            "buyer_fee_gbp": round(total - price, 2),
            "favourite_count": int(fav_txt) if fav_txt.isdigit() else 0,
            "url": f"https://www.vinted.co.uk/items/{m_id.group(1)}",
        })
    return out


def age_interval_hours(text: str | None) -> tuple[float, float] | None:
    """'3 weeks ago' -> (17.5*24, 28*24). Bounds cover both floor and round display conventions."""
    if not text:
        return None
    m = RE_AGE.search(text)
    if not m:
        return (0.0, 1 / 60) if re.search(r"just now|seconds? ago", text, re.I) else None
    n_raw = m["n"].lower()
    n = 1 if n_raw in ("a", "an") else (3 if n_raw == "a few" else int(n_raw))
    u = UNIT_HOURS[m["unit"].lower()]
    return (max(n - 0.5, 0) * u, (n + 1) * u)


def parse_item_page(html: str | None, http_status: int) -> dict:
    """Status of one item page: 'active', 'sold', 'deleted' or 'unknown' (hidden / reserved)."""
    if http_status == 404 or html is None:
        return {"status": "deleted", "age_hours": None}
    soup = BeautifulSoup(html, "html.parser")
    avail = None
    ld = soup.select_one('script[type="application/ld+json"]')
    if ld and ld.string:
        try:
            avail = (json.loads(ld.string).get("offers") or {}).get("availability")
        except (json.JSONDecodeError, AttributeError):
            pass
    up = soup.select_one('[itemprop="upload_date"]')
    age = age_interval_hours(up.get_text(strip=True) if up else None)
    has_buy = soup.select_one('[data-testid="item-buy-button"]') is not None
    if avail == "InStock" and has_buy:
        status = "active"
    elif avail is None and re.search(r"\bSold\b", soup.get_text(" ")):
        status = "sold"
    else:
        status = "unknown"
    return {"status": status, "age_hours": age}
