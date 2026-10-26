"""Vinted parser + calibration tests. Catalogue cards mirror real Vinted UK markup (24 Oct 2026);
the upload samples in fixtures/ are real item pages read that evening."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.models.schemas import ListingRow
from backend.scrapers.vinted_calibration import VintedCalibration
from backend.scrapers.vinted_client import active_rows
from backend.scrapers.vinted_parse import age_interval_hours, parse_catalog, parse_item_page

FIX = Path(__file__).parent / "fixtures"


def grid(lid, summary, fav=""):
    return (f'<div data-testid="grid-item"><a href="/items/{lid}-slug?referrer=catalog" title="{summary}"></a>'
            f'<p data-testid="product-item-id-{lid}--price-text">£x</p>'
            f'<span data-testid="favourite-count-text">{fav}</span></div>')


CATALOG = "<div>" + "".join([
    grid("10517348707", "Nike air force 1 size 7, Brand: Nike, Condition: Good, Size: 7, 18.00 £, 19.60 £"),
    grid("10517250083", "nike airs (air force 1) great condition, Brand: Nike, Condition: Very good, "
                        "Size: 6 junior | EU 39, 20.00 £, 21.90 £", fav="2"),
    grid("10484836453", "Air Force 1’s, Brand: Nike, Condition: Good, Size: 10.5, 30.24 £, 32.45 £"),
    grid("10517348707", "duplicate card, Brand: Nike, Condition: Good, Size: 7, 18.00 £, 19.60 £"),
    grid("10400000000", "Headphones, Brand: Sony, Condition: New with tags, 1,050.00 £, 1,103.20 £"),  # no size
]) + "</div>"


def test_parse_catalog():
    cards = parse_catalog(CATALOG)
    assert [c["item_id"] for c in cards] == ["10517348707", "10517250083", "10484836453", "10400000000"]
    assert cards[0]["price_gbp"] == 18.0 and cards[0]["buyer_fee_gbp"] == 1.6  # 5% + £0.70
    assert cards[1]["size"] == "6 junior | EU 39" and cards[1]["favourite_count"] == 2
    assert cards[1]["condition_raw"] == "Very good"
    assert cards[3]["size"] is None and cards[3]["price_gbp"] == 1050.0


@pytest.mark.parametrize("text,lo,hi", [
    ("42 min ago", 41.5 / 60, 43 / 60), ("an hour ago", 0.5, 2), ("9 hours ago", 8.5, 10),
    ("a day ago", 12, 48), ("6 days ago", 132, 168), ("a week ago", 84, 336), ("3 weeks ago", 420, 672),
])
def test_age_phrases(text, lo, hi):
    a = age_interval_hours(text)
    assert a == pytest.approx((lo, hi))


def test_item_page_status():
    live = ('<script type="application/ld+json">{"offers":{"availability":"InStock"}}</script>'
            '<div itemprop="upload_date">2 min ago</div><button data-testid="item-buy-button">Buy now</button>')
    sold = '<div itemprop="upload_date">2 weeks ago</div><div>Sold</div>'
    assert parse_item_page(live, 200)["status"] == "active"
    assert parse_item_page(sold, 200)["status"] == "sold"
    assert parse_item_page(None, 404)["status"] == "deleted"
    assert parse_item_page("<div>Reserved</div>", 200)["status"] == "unknown"


@pytest.fixture
def real_cal(tmp_path):
    data = json.loads((FIX / "vinted_upload_samples.json").read_text())
    seen = datetime.fromisoformat(data["observed_at"].replace("Z", "+00:00"))
    cal = VintedCalibration(tmp_path / "cal.json")
    for iid, phrase in data["samples"]:
        cal.add_age(iid, age_interval_hours(phrase), seen)
    return cal, seen


def test_real_samples_are_monotone(real_cal):
    """Older IDs must never have a later earliest-start than newer IDs (counter only goes up)."""
    cal, _ = real_cal
    ids = sorted(cal.samples)
    lows = [cal.samples[i][0] for i in ids]
    highs = [cal.samples[i][1] for i in ids]
    # every sample's window overlaps the running envelope, i.e. the data is consistent
    for k in range(1, len(ids)):
        assert highs[k] >= max(lows[:k])


def test_calibration_bounds_from_real_samples(real_cal):
    cal, seen = real_cal
    b = cal.bounds(10450000000)  # between the "4 days" and "6 days" samples
    age_lo = (seen - b.hi).total_seconds() / 86400
    age_hi = (seen - b.lo).total_seconds() / 86400
    assert 3.4 < age_lo < age_hi < 7.1 and b.lo <= b.mid <= b.hi
    assert cal.bounds(9_000_000_000) is None  # older than any sample


def test_active_rows_validate(real_cal):
    cal, seen = real_cal
    cal.add_exact(10517348707, seen, slack_minutes=60)
    rows = active_rows(parse_catalog(CATALOG), cal, seen + timedelta(minutes=5))
    assert {r["listing_id"] for r in rows} == {"10517348707", "10517250083", "10484836453", "10400000000"}
    newest = next(r for r in rows if r["listing_id"] == "10517348707")
    assert newest["t_days"] < 0.01
    for r in rows:
        ListingRow.model_validate(r)


def test_bumped_listing_sample_is_ignored(tmp_path):
    """Real samples from 24 Oct 2026 (UTC): the oldest ID was a bumped listing whose page said
    'Uploaded 27 min ago'. Without pruning, every listing came out ~0 days old."""
    t = lambda d, hm: datetime(2026, 10, d, int(hm[:2]), int(hm[3:]), tzinfo=timezone.utc)  # noqa: E731
    cal = VintedCalibration(tmp_path / "c.json")
    for iid, (d1, a), (d2, b) in [
        (10482200093, (24, "21:01"), (24, "21:03")),   # bumped: contradicts everything newer
        (10484378826, (21, "21:28"), (23, "09:28")),
        (10492646366, (22, "21:28"), (24, "09:28")),
        (10510472775, (24, "13:28"), (24, "14:58")),
        (10514379106, (24, "16:28"), (24, "17:58")),
        (10516428018, (24, "19:28"), (24, "20:58")),
        (10517596608, (24, "20:28"), (24, "21:28")),
    ]:
        cal.samples[iid] = (t(d1, a).timestamp(), t(d2, b).timestamp())
    assert 10482200093 not in cal.consistent_ids()
    seen = t(24, "21:28")
    b = cal.bounds(10485345145)  # just newer than the "2 days ago" sample
    age_lo = (seen - b.hi).total_seconds() / 86400
    age_hi = (seen - b.lo).total_seconds() / 86400
    assert 0.4 < age_lo < age_hi < 3.1


def test_session_refresh_placeholder_is_recognised():
    from backend.scrapers.vinted_parse import is_session_refresh
    html = '<html><head><meta charSet="utf-8"/><title>Session refresh</title></head><body></body></html>'
    assert is_session_refresh({"final_url": "https://www.vinted.co.uk/session-refresh?ref_url=%2Fitems%2F1", "html": ""})
    assert is_session_refresh({"final_url": "https://www.vinted.co.uk/items/10524334280", "html": html})
    assert not is_session_refresh({"final_url": "https://www.vinted.co.uk/items/10524334280",
                                   "html": "<title>Nike Air Jordan 1 | Vinted</title>"})
    assert not is_session_refresh({"final_url": "https://www.vinted.co.uk/catalog?search_text=session-refresh",
                                   "html": "<title>session-refresh | Vinted</title>"})
