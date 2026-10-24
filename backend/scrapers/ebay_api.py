"""Official eBay Browse API client (developer.ebay.com, Buy > Browse API).

Used for ACTIVE listings when EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are set in .env.
Sold listings aren't in the public API, so those still come from the sold-search page.

What we use (checked against the published spec, 24 Oct 2026):
  auth    client-credentials OAuth -> application token, scope https://api.ebay.com/oauth/api_scope
  search  GET /buy/browse/v1/item_summary/search?q=...&sort=newlyListed&limit=200&offset=...
          header X-EBAY-C-MARKETPLACE-ID: EBAY_GB; at most 10,000 results per query
  fields  legacyItemId (the 12-digit item number), title, price, condition, shippingOptions,
          itemLocation, itemCreationDate (this listing's start) and itemOriginDate (first
          listed; kept across relists, so it is the right start for time-to-sell)

Each result is also a calibration point (item number, itemCreationDate) for dating sold items.
Returns "card" dicts in the same shape as ebay_parse.parse_cards, so ebay_client works unchanged.

Owner: Person 2.
"""
from __future__ import annotations

import base64
import os
import time
from datetime import datetime

import httpx

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
SCOPE = "https://api.ebay.com/oauth/api_scope"


def configured() -> bool:
    return bool(os.getenv("EBAY_CLIENT_ID") and os.getenv("EBAY_CLIENT_SECRET"))


class BrowseAPI:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None, client: httpx.Client | None = None):
        self.client_id = client_id or os.environ["EBAY_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["EBAY_CLIENT_SECRET"]
        self.http = client or httpx.Client(timeout=20)
        self._token, self._expires = None, 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        r = self.http.post(TOKEN_URL, headers={"Authorization": f"Basic {basic}",
                                               "Content-Type": "application/x-www-form-urlencoded"},
                           data={"grant_type": "client_credentials", "scope": SCOPE})
        r.raise_for_status()
        j = r.json()
        self._token, self._expires = j["access_token"], time.time() + float(j.get("expires_in", 7200))
        return self._token

    def search(self, q: str, limit: int = 200, offset: int = 0, sort: str = "newlyListed") -> dict:
        r = self.http.get(SEARCH_URL, params={"q": q, "limit": limit, "offset": offset, "sort": sort},
                          headers={"Authorization": f"Bearer {self.token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_GB"})
        r.raise_for_status()
        return r.json()

    def active_cards(self, q: str, pages: int = 3) -> list[dict]:
        out = []
        for p in range(pages):
            j = self.search(q, offset=p * 200)
            items = j.get("itemSummaries", [])
            out += [c for c in (to_card(it) for it in items) if c]
            if len(items) < 200:
                break
        return out


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def to_card(it: dict) -> dict | None:
    lid = it.get("legacyItemId") or (it.get("itemId", "").split("|")[1] if "|" in it.get("itemId", "") else None)
    price = (it.get("price") or {})
    if not lid or price.get("currency") != "GBP":
        return None
    ship = None
    for opt in it.get("shippingOptions") or []:
        cost = (opt.get("shippingCost") or {}).get("value")
        if cost is not None:
            ship = float(cost)
            break
    created, origin = _ts(it.get("itemCreationDate")), _ts(it.get("itemOriginDate"))
    country = (it.get("itemLocation") or {}).get("country")
    return {
        "item_id": str(lid), "title": it.get("title", ""), "price_gbp": float(price["value"]),
        "shipping_gbp": ship, "condition_raw": it.get("condition"),
        "start_time": origin or created,    # first listed: right start for time-to-sell
        "created_time": created,            # this item number's own start: right for calibration
        "sold_date": None, "best_offer_accepted": False,
        "is_auction": "AUCTION" in (it.get("buyingOptions") or []),
        "from_abroad": bool(country and country != "GB"),
        "url": it.get("itemWebUrl") or f"https://www.ebay.co.uk/itm/{lid}",
    }
