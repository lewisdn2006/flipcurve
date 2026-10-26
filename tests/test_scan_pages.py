"""End to end: pages as the helper extension sends them -> /api/v1/scan-pages -> full payload."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from backend.models.schemas import MarketScanResponse
from tests.test_ebay import card

T0 = datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)
ITEM = {"title": "Nike Air Force 1 '07 White", "brand": "Nike", "model_name": None, "category": None,
        "size_or_storage": None, "era_or_year": None, "condition_tier": "good", "condition_score": 7.0,
        "detected_flaws": [], "search_queries": {"ebay": "nike air force 1 white", "vinted": "air force 1 white"},
        "is_broken": False}


def ebay_active_html(now):
    # 60 listings in one item-number block, one an hour going back from `now` (UK time shown)
    out = []
    for i in range(60):
        t = (now - timedelta(hours=i)).astimezone()
        uk = (now - timedelta(hours=i)) + timedelta(hours=1)  # BST
        out.append(card(str(336744200000 - i * 10_000), "Nike Air Force 1 white trainers", f"{30 + i % 9}.00",
                        ["Pre-owned", "+£3.38 delivery", uk.strftime("%d-%b %H:%M")]))
    return "<ul>" + "".join(out) + "</ul>"


def ebay_sold_html(now):
    out = []
    for i in range(30):
        sold_day = (now - timedelta(hours=2 * i)).strftime("%d %b %Y").lstrip("0")
        out.append(card(str(336744195000 - i * 15_000), "Nike Air Force 1 white", f"{26 + i % 7}.00",
                        [f"Sold {sold_day}", "Pre-owned", "+£3.38 delivery"]))
    return "<ul>" + "".join(out) + "</ul>"


def vinted_grid(lid, price):
    return (f'<div data-testid="grid-item"><a href="/items/{lid}-slug" title="Nike Air Force 1 white, Brand: Nike, '
            f'Condition: Good, Size: 9, {price:.2f} £, {price * 1.05 + 0.7:.2f} £"></a>'
            f'<span data-testid="favourite-count-text"></span></div>')


def vinted_item(age_text, sold=False):
    if sold:
        return f'<div itemprop="upload_date">{age_text}</div><div>Sold</div>'
    return ('<script type="application/ld+json">{"offers":{"availability":"InStock"}}</script>'
            f'<div itemprop="upload_date">{age_text}</div><button data-testid="item-buy-button">Buy</button>')


def page(platform, kind, html, when, url="https://x/", n=1):
    return {"platform": platform, "kind": kind, "page": n, "url": url, "final_url": url, "status": 200,
            "html": html, "fetched_at": when.isoformat()}


@pytest.fixture
def client(tmp_path, monkeypatch):
    import backend.scrapers.orchestrator as orch
    import backend.scrapers.page_ingest as ing
    from backend.scrapers.ebay_calibration import CalibrationStore
    from backend.scrapers.vinted_calibration import VintedCalibration
    monkeypatch.setattr(orch, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(ing, "CalibrationStore", lambda: CalibrationStore(tmp_path / "cal.sqlite"))
    monkeypatch.setattr(ing, "VintedCalibration", lambda: VintedCalibration(tmp_path / "vcal.json"))
    from backend.main import app
    return TestClient(app)


def first_scan(when):
    ids = [10517000000 - i * 900_000 for i in range(10)]  # ~one an hour back from `when`
    cat = "<div>" + "".join(vinted_grid(i, 20 + n) for n, i in enumerate(ids)) + "</div>"
    return [
        page("ebay", "active", ebay_active_html(when), when),
        page("ebay", "sold", ebay_sold_html(when), when),
        page("vinted", "catalog", cat, when),
        page("vinted", "item", vinted_item("an hour ago"), when, url=f"https://www.vinted.co.uk/items/{ids[1]}"),
        page("vinted", "item", vinted_item("9 hours ago"), when, url=f"https://www.vinted.co.uk/items/{ids[9]}"),
    ], ids


def test_scan_pages_end_to_end_and_vinted_recheck(client):
    pages, ids = first_scan(T0)
    r = client.post("/api/v1/scan-pages", json={"item": ITEM, "pages": pages})
    assert r.status_code == 200, r.text
    out = MarketScanResponse.model_validate(r.json())
    ev = out.comparable_listings
    assert any(x.platform == "ebay" and x.obs_type == "sold_interval" for x in ev)
    assert any(x.platform == "vinted" for x in ev)
    assert out.market_summary.data_source == "live"

    # nothing is due for a re-check straight away...
    assert client.get("/api/v1/recheck-ids", params={"vinted_query": "air force 1 white"}).status_code == 200

    # ...a second scan 8 hours later re-checks two of them: one sold, one still listed
    later = T0 + timedelta(hours=8)
    pages2, _ = first_scan(later)
    pages2 += [page("vinted", "recheck", vinted_item("8 hours ago", sold=True), later,
                    url=f"https://www.vinted.co.uk/items/{ids[2]}"),
               page("vinted", "recheck", vinted_item("12 hours ago"), later,
                    url=f"https://www.vinted.co.uk/items/{ids[5]}")]
    r2 = client.post("/api/v1/scan-pages", json={"item": ITEM, "pages": pages2})
    assert r2.status_code == 200, r2.text
    out2 = MarketScanResponse.model_validate(r2.json())
    vint = [x for x in out2.comparable_listings if x.platform == "vinted"]
    assert any(x.obs_type == "sold_interval" for x in vint)   # a real Vinted sale, with entry age
    assert out2.platform_recommendations["vinted"].n_sales >= 1


def test_stopped_sites_become_clear_warnings(client):
    pages, _ = first_scan(T0)
    pages = [p for p in pages if not (p["platform"] == "ebay" and p["kind"] == "sold")]
    r = client.post("/api/v1/scan-pages", json={"item": ITEM, "pages": pages,
                                                 "stopped": {"ebay": {"reason": "sign_in_required"}}})
    assert r.status_code == 200
    assert any("signed in" in w for w in r.json()["warnings"])


def test_scan_pages_accepts_gzip(client):
    import gzip
    import json
    pages, _ = first_scan(T0)
    body = gzip.compress(json.dumps({"item": ITEM, "pages": pages}).encode())
    r = client.post("/api/v1/scan-pages", content=body,
                    headers={"Content-Type": "application/json", "Content-Encoding": "gzip"})
    assert r.status_code == 200, r.text
    assert MarketScanResponse.model_validate(r.json()).market_summary.data_source == "live"


def test_related_versions_help_speed_but_not_price(client):
    from backend.engine.outlier_filter import version_mismatch
    assert version_mismatch("Nike Air Jordan 1 Mid Triple Black UK 9", "air jordan 1 low triple black") == "mid"
    assert version_mismatch("Nike Air Jordan 1 Low (GS) Triple Black", "air jordan 1 low triple black") == "kids"
    assert version_mismatch("iPhone 13 Pro Max 128GB", "iphone 13 pro") == "max"
    assert version_mismatch("Nike Air Jordan 1 Low Triple Black UK 10", "air jordan 1 low triple black") is None


def test_background_rechecks_find_sales_without_a_new_scan(client):
    from backend.scrapers import page_ingest as ing
    pages, ids = first_scan(T0)
    assert client.post("/api/v1/scan-pages", json={"item": ITEM, "pages": pages}).status_code == 200
    # nothing is due straight away; 4 hours later the matching listings are
    assert ing.due_rechecks(now=T0 + timedelta(hours=1)) == []
    due = ing.due_rechecks(now=T0 + timedelta(hours=4))
    assert due and all(d["query"] == "air force 1 white" for d in due)
    assert set(d["id"] for d in due) <= set(map(str, ids))
    assert client.get("/api/v1/recheck-due").status_code == 200

    later = T0 + timedelta(hours=4)
    sold_id, live_id = due[0]["id"], due[1]["id"]
    res = [{"query": "air force 1 white", "page": page("vinted", "recheck", vinted_item("4 hours ago", sold=True), later,
                                                        url=f"https://www.vinted.co.uk/items/{sold_id}")},
           {"query": "air force 1 white", "page": page("vinted", "recheck", vinted_item("5 hours ago"), later,
                                                        url=f"https://www.vinted.co.uk/items/{live_id}")}]
    r = client.post("/api/v1/recheck-results", json={"results": res})
    assert r.status_code == 200 and r.json()["sold"] == 1 and r.json()["active"] == 1, r.text
    # the sale is in the saved rows at once, and that listing is no longer due
    from backend.scrapers.orchestrator import load_cache
    saved = load_cache("air force 1 white", "vinted")
    assert any(x["listing_id"] == sold_id and x["obs_type"] == "sold_interval" for x in saved)
    assert sold_id not in {d["id"] for d in ing.due_rechecks(now=later + timedelta(hours=7))}
    # "Use saved data" now has a real Vinted sale
    out = client.post("/api/v1/market-scan", json={"item": ITEM, "allow_live": False}).json()
    assert out["platform_recommendations"]["vinted"]["n_sales"] >= 1


def test_ebay_api_adds_active_listings_and_calibration(client, monkeypatch):
    """With eBay keys set, the server adds Browse API listings (exact dates) to the helper's pages."""
    import backend.scrapers.ebay_api as api
    calls = []

    class FakeAPI:
        def active_cards(self, q, pages=5):
            calls.append(q)
            return [{"item_id": str(336744300000 + i * 10_000), "title": "Nike Air Force 1 white trainers",
                     "price_gbp": 32.0 + i % 5, "shipping_gbp": 3.0, "condition_raw": "Pre-owned",
                     "start_time": T0 - timedelta(hours=3 * i), "created_time": T0 - timedelta(hours=3 * i),
                     "sold_date": None, "best_offer_accepted": False, "is_auction": False, "from_abroad": False,
                     "url": f"https://www.ebay.co.uk/itm/{336744300000 + i * 10_000}"} for i in range(40)]
    monkeypatch.setattr(api, "configured", lambda: True)
    monkeypatch.setattr(api, "BrowseAPI", FakeAPI)
    pages, _ = first_scan(T0)
    r = client.post("/api/v1/scan-pages", json={"item": ITEM, "pages": pages})
    assert r.status_code == 200, r.text
    assert calls == ["nike air force 1 white"]
    ids = {x["listing_id"] for x in r.json()["comparable_listings"] if x["platform"] == "ebay"}
    assert any(i.startswith("3367443") for i in ids)          # API listings are in
    assert client.get("/api/v1/health").json()["ebay_api"] is True


def test_ebay_api_failure_falls_back_to_helper_pages(client, monkeypatch):
    import backend.scrapers.ebay_api as api

    class Broken:
        def active_cards(self, q, pages=5):
            raise RuntimeError("401 invalid_client")
    monkeypatch.setattr(api, "configured", lambda: True)
    monkeypatch.setattr(api, "BrowseAPI", Broken)
    pages, _ = first_scan(T0)
    out = client.post("/api/v1/scan-pages", json={"item": ITEM, "pages": pages}).json()
    assert any("eBay API" in w for w in out["warnings"])
    assert any(x["platform"] == "ebay" for x in out["comparable_listings"])


SESSION_REFRESH_HTML = '<!DOCTYPE html><html lang="en"><head><title>Session refresh</title></head><body></body></html>'


def session_refresh(kind, url, when):
    p = page("vinted", kind, SESSION_REFRESH_HTML, when, url=url)
    p["final_url"] = "https://www.vinted.co.uk/session-refresh?ref_url=" + url.split(".co.uk")[1].replace("/", "%2F")
    return p


def test_vinted_session_refresh_pages_are_left_out_with_a_warning(client):
    """26 Oct: an expired Vinted session sent every page to /session-refresh. Older helpers pass the
    placeholder on; it must not count as a listing, a sale or a re-check."""
    pages, ids = first_scan(T0)
    assert client.post("/api/v1/scan-pages", json={"item": ITEM, "pages": pages}).status_code == 200
    later = T0 + timedelta(hours=8)
    ebay_only = [p for p in first_scan(later)[0] if p["platform"] == "ebay"]
    placeholders = [session_refresh("catalog", "https://www.vinted.co.uk/catalog?search_text=air+force+1+white", later),
                    session_refresh("recheck", f"https://www.vinted.co.uk/items/{ids[2]}", later)]
    r = client.post("/api/v1/scan-pages", json={"item": ITEM, "pages": ebay_only + placeholders})
    assert r.status_code == 200, r.text
    warnings = r.json()["warnings"]
    assert any('2 Vinted pages came back as Vinted\'s "session refresh" page' in w for w in warnings), warnings
    assert any("saved Vinted data" in w for w in warnings)   # the first scan's Vinted rows stand in
    from backend.scrapers.page_ingest import _load_vinted_cache
    rec = _load_vinted_cache("air force 1 white")["seen"][str(ids[2])]
    assert "last_checked" not in rec and not rec.get("resolved")   # still due, not "checked"


def test_new_helper_stop_reason_has_its_own_warning(client):
    pages = [p for p in first_scan(T0)[0] if p["platform"] == "ebay"]
    r = client.post("/api/v1/scan-pages", json={"item": ITEM, "pages": pages,
                                                 "stopped": {"vinted": {"reason": "session_expired"}}})
    assert r.status_code == 200
    assert any("Open vinted.co.uk in this browser" in w for w in r.json()["warnings"])
    assert not any("came back as" in w for w in r.json()["warnings"])


def test_background_rechecks_skip_session_refresh_pages(client):
    from backend.scrapers import page_ingest as ing
    pages, ids = first_scan(T0)
    assert client.post("/api/v1/scan-pages", json={"item": ITEM, "pages": pages}).status_code == 200
    later = T0 + timedelta(hours=4)
    due = ing.due_rechecks(now=later)
    res = [{"query": "air force 1 white",
            "page": session_refresh("recheck", f"https://www.vinted.co.uk/items/{d['id']}", later)} for d in due[:2]]
    r = client.post("/api/v1/recheck-results", json={"results": res})
    assert r.status_code == 200 and r.json()["session_refresh"] == 2 and r.json()["unknown"] == 0, r.text
    assert {d["id"] for d in due[:2]} <= {d["id"] for d in ing.due_rechecks(now=later)}   # still due
