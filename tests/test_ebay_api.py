"""Browse API client against canned responses shaped like the published spec."""
import httpx

from backend.scrapers.ebay_api import BrowseAPI, to_card

ITEM = {"itemId": "v1|336744126355|0", "legacyItemId": "336744126355", "title": "Nike Air Force 1 07 White",
        "price": {"value": "34.99", "currency": "GBP"}, "condition": "Pre-owned",
        "shippingOptions": [{"shippingCost": {"value": "3.49", "currency": "GBP"}}],
        "itemLocation": {"country": "GB"}, "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
        "itemCreationDate": "2026-10-20T10:00:00.000Z", "itemOriginDate": "2026-10-01T09:00:00.000Z",
        "itemWebUrl": "https://www.ebay.co.uk/itm/336744126355"}


def test_to_card_uses_origin_for_start_and_creation_for_calibration():
    c = to_card(ITEM)
    assert c["item_id"] == "336744126355" and c["price_gbp"] == 34.99 and c["shipping_gbp"] == 3.49
    assert c["start_time"].day == 1 and c["created_time"].day == 20 and not c["from_abroad"]
    assert to_card({**ITEM, "price": {"value": "10", "currency": "USD"}}) is None


def test_token_and_search_flow():
    calls = []

    def handler(req: httpx.Request):
        calls.append(req)
        if req.url.path.endswith("/oauth2/token"):
            assert req.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "T", "expires_in": 7200})
        assert req.headers["Authorization"] == "Bearer T"
        assert req.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_GB"
        assert req.url.params["sort"] == "newlyListed"
        return httpx.Response(200, json={"itemSummaries": [ITEM]})

    api = BrowseAPI("id", "secret", client=httpx.Client(transport=httpx.MockTransport(handler)))
    cards = api.active_cards("nike air force 1")
    assert len(cards) == 1 and len(calls) == 2  # one token, one search page (fewer than 200 -> stop)
