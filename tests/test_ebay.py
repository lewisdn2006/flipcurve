"""eBay parser + calibration tests, using cards shaped like real eBay UK results (24 Oct 2026)."""
from datetime import date, datetime, timedelta, timezone

from backend.models.schemas import ListingRow
from backend.scrapers.ebay_calibration import CalibrationStore
from backend.scrapers.ebay_client import active_rows, sold_rows
from backend.scrapers.ebay_parse import UK, condition_tier, parse_cards

NOW = datetime(2026, 10, 24, 21, 0, tzinfo=timezone.utc)  # 22:00 UK time


def card(lid, title, price, extra):
    spans = "".join(f"<span>{t}</span>" for t in extra)
    return (f'<li class="s-card" data-listingid="{lid}"><a class="s-card__link" href="https://www.ebay.co.uk/itm/{lid}"></a>'
            f'<div class="s-card__title"><span>{title}</span><span>Opens in a new window or tab</span></div>'
            f'<div class="s-card__subtitle-row"><span>UK 9 ·</span><span>Nike</span></div>{spans[:0]}'
            f'<span class="su-styled-text primary bold large-1 s-card__price">£{price}</span>{spans}</li>')


ACTIVE_HTML = "<ul>" + "".join([
    card("960110784126", "Size 11.5 - Nike Air Force 1 '07 Low Triple White", "53.83",
         ["Pre-owned", "or Best Offer", "+£28.00 delivery", "from United States", "24-Oct 21:48"]),
    card("227582015253", "Nike Air Force 1 Low Glitter Swoosh", "134.59",
         ["New (other)", "or Best Offer", "Postage not specified", "24-Oct 21:47"]),
    card("206651183451", "Nike Force 1 Fontanka PS White", "17.20",
         ["Pre-owned", "or Best Offer", "+£3.38 delivery", "31-Dec 23:59"]),  # no year: must be last year
    '<li data-listingid="2500219655424533"><div class="s-card__title">Shop on eBay</div>'
    '<span class="s-card__price">£20.00</span></li>',                     # sponsored placeholder
    card("123456789012", "Variation listing", "10.00 to £20.00", ["Brand new", "24-Oct 20:00"]),
]) + "</ul>"

SOLD_HTML = "<ul>" + "".join([
    card("257795184094", "Nike Air Force 1 '07 LV8 Men's size Uk 10", "68.30",
         ["Sold 24 Oct 2026", "Brand new", "Buy It Now", "+£3.38 delivery"]),
    card("336744126355", "Nike Air Force 1 Low SP Court Blue", "40.38",
         ["Sold 23 Oct 2026", "Pre-owned", "Best Offer accepted", "+£3.66 delivery"]),
    card("999000000001", "Undatable item", "12.00", ["Sold 20 Oct 2026", "Pre-owned"]),
]) + "</ul>"


def test_parse_active_cards():
    cards = parse_cards(ACTIVE_HTML, NOW)
    assert [c["item_id"] for c in cards] == ["960110784126", "227582015253", "206651183451"]
    a = cards[0]
    assert a["title"] == "Size 11.5 - Nike Air Force 1 '07 Low Triple White"
    assert a["price_gbp"] == 53.83 and a["shipping_gbp"] == 28.0 and a["from_abroad"]
    assert a["start_time"] == datetime(2026, 10, 24, 21, 48, tzinfo=UK)
    assert cards[1]["shipping_gbp"] is None and cards[1]["condition_raw"] == "New (other)"
    assert cards[2]["start_time"].year == 2025


def test_parse_sold_cards():
    cards = parse_cards(SOLD_HTML, NOW)
    assert cards[0]["sold_date"] == date(2026, 10, 24) and not cards[0]["best_offer_accepted"]
    assert cards[1]["best_offer_accepted"] and cards[1]["sold_date"] == date(2026, 10, 23)


def test_condition_mapping():
    assert condition_tier("Brand new") == "mint"
    assert condition_tier("Pre-owned") == "good"
    assert condition_tier("Pre-owned - Fair") == "fair"
    assert condition_tier("For parts or not working") is None


def _store(tmp_path):
    s = CalibrationStore(tmp_path / "cal.sqlite")
    # block 336: one id every 10,000 = 1 hour, ending at 21:00 UTC on 24 Oct
    t_end = NOW
    pts = [(str(336744000000 + i * 10_000), t_end - timedelta(hours=(30 - i))) for i in range(31)]
    pts += [(str(257795000000 + i * 10_000), t_end - timedelta(days=(30 - i))) for i in range(31)]
    s.add(pts)
    return s


def test_calibration_brackets_and_accuracy(tmp_path):
    s = _store(tmp_path)
    est = s.estimate_start("336744126355")  # 12.6 steps in -> 12.6 h after the first point
    first = NOW - timedelta(hours=30)
    assert abs((est.mid - (first + timedelta(hours=12.6355))).total_seconds()) < 60
    assert est.lo <= est.mid <= est.hi and est.gap_days < 0.05
    assert s.estimate_start("999000000001") is None       # unknown block
    assert s.estimate_start("336000000000") is None       # older than the calibration
    assert max(s.leave_one_out_error_hours()) < 1e-6      # perfectly linear test data


def test_rows_validate_and_intervals_make_sense(tmp_path):
    s = _store(tmp_path)
    a = active_rows(parse_cards(ACTIVE_HTML, NOW), NOW)
    assert [r["listing_id"] for r in a] == ["227582015253", "206651183451"]  # US import dropped
    assert a[0]["t_days"] < 0.01 and a[0]["start_source"] == "exact"
    sold = sold_rows(parse_cards(SOLD_HTML, NOW), s, NOW)
    by_id = {r["listing_id"]: r for r in sold}
    blue = by_id["336744126355"]  # listed ~24 Oct 03:38 UTC, sold some time on 23 Oct UK -> impossible
    assert blue["obs_type"] == "sold_unknown_time"  # calibration says listed after its sold day
    # block 257 has one point a day: listed between 12 and 11 days before NOW (~11.6), sold some
    # time on 24 Oct UK (23:00 UTC 23 Oct to 23:00 UTC 24 Oct) -> sold after ~10 to ~12.2 days
    lv8 = by_id["257795184094"]
    assert lv8["obs_type"] == "sold_interval" and 9.5 < lv8["t_days"] < lv8["t_upper_days"] < 12.5
    assert by_id["999000000001"]["obs_type"] == "sold_unknown_time"
    for r in a + sold:
        ListingRow.model_validate(r)


def test_block_detection_uses_title_only():
    from backend.scrapers.fetcher import looks_blocked
    normal = "<html><head><title>Nike Air Force 1 for sale | eBay</title><script>var captchaCfg={}</script></head></html>"
    block = "<html><head><title>Pardon Our Interruption...</title></head></html>"
    assert not looks_blocked(normal) and looks_blocked(block)
