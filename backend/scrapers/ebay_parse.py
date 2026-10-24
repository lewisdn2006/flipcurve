"""Parse eBay UK search-result HTML into plain card dicts (no network, easy to test).

Card structure as of 24 Oct 2026: each result is <li data-listingid="..."> with the title in
.s-card__title and the price in .s-card__price. Everything else is read from the card's
text pieces with regexes, which survives most markup changes:

  newest-first active search:  "... | £53.83 | or Best Offer | +£28.00 delivery | 24-Oct 21:48"
  sold search:                 "Sold 24 Oct 2026 | ... | £40.38 | Best Offer accepted | +£33.66 delivery"

Owner: Person 2.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

UK = ZoneInfo("Europe/London")
MONTHS = {m: i + 1 for i, m in enumerate("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split())}

RE_START = re.compile(r"\b(\d{1,2})-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)(?:-(\d{2}))? (\d{2}):(\d{2})\b")
RE_SOLD = re.compile(r"\bSold\s+(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4})\b")
RE_PRICE = re.compile(r"£\s?([\d,]+\.\d{2})")
RE_DELIVERY = re.compile(r"\+£\s?([\d,]+\.\d{2}) (?:delivery|postage)")
RE_BIDS = re.compile(r"\b(\d+) bids?\b")
CONDITIONS = [  # longest first so "Pre-owned - Excellent" wins over "Pre-owned"
    "Pre-owned - Excellent", "Pre-owned - Good", "Pre-owned - Fair", "New with tags", "New without tags",
    "New with box", "New (other)", "Opened – never used", "Brand new", "Pre-owned", "Used",
    "For parts or not working", "Parts only",
]


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _start_time(m: re.Match, now: datetime) -> datetime:
    """'24-Oct 21:48' is UK local time with no year: take the most recent such moment <= now."""
    day, mon, yy, hh, mm = m.groups()
    year = 2000 + int(yy) if yy else now.astimezone(UK).year
    t = datetime(year, MONTHS[mon], int(day), int(hh), int(mm), tzinfo=UK)
    if not yy and t > now.astimezone(UK):
        t = t.replace(year=t.year - 1)
    return t


def parse_cards(html: str, now: datetime) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = []
    for li in soup.select("li[data-listingid]"):
        lid = li.get("data-listingid", "")
        if not (lid.isdigit() and len(lid) == 12):
            continue  # sponsored placeholders carry other ids
        title_el = li.select_one(".s-card__title")
        price_el = li.select_one(".s-card__price")
        if title_el is None or price_el is None:
            continue
        title = title_el.get_text(" ", strip=True).replace("Opens in a new window or tab", "").strip()
        price_txt = price_el.get_text(" ", strip=True)
        if " to " in price_txt:  # variation listings show a price range; skip them
            continue
        pm = RE_PRICE.search(price_txt)
        if not pm:
            continue
        text = " | ".join(s for s in li.stripped_strings)
        start = RE_START.search(text)
        sold = RE_SOLD.search(text)
        ship = RE_DELIVERY.search(text)
        bids = RE_BIDS.search(text)
        cond = next((c for c in CONDITIONS if c in text), None)
        cards.append({
            "item_id": lid,
            "title": title,
            "price_gbp": _num(pm.group(1)),
            "shipping_gbp": _num(ship.group(1)) if ship else (0.0 if "Free delivery" in text or "Free postage" in text else None),
            "condition_raw": cond,
            "start_time": _start_time(start, now) if start else None,
            "sold_date": date(int(sold.group(3)), MONTHS[sold.group(2)], int(sold.group(1))) if sold else None,
            "best_offer_accepted": "Best Offer accepted" in text,
            "is_auction": bool(bids),
            "from_abroad": bool(re.search(r"\bfrom (?!United Kingdom)[A-Z][a-z]+", text)),
            "url": f"https://www.ebay.co.uk/itm/{lid}",
        })
    return cards


def condition_tier(raw: str | None) -> str | None:
    """Map eBay's condition text to our 4 tiers (None = unknown; the engine treats it as 'good')."""
    if raw is None:
        return None
    if raw in ("Brand new", "New", "New with tags", "New with box", "New without tags"):
        return "mint"
    if raw in ("New (other)", "Opened – never used", "Pre-owned - Excellent"):
        return "very_good"
    if raw in ("Pre-owned - Good", "Pre-owned", "Used", "Good"):
        return "good"
    if raw == "Pre-owned - Fair":
        return "fair"
    return None  # parts / not working: the outlier filter drops these unless the item is broken
