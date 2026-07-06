"""Recipe interpreter tests - offline fixture replay (H3.4): fetch_page is
injected, no network."""

from datetime import datetime

from eventindex.fetch.recipe import (
    Pagination, Recipe, extract_with_selectors, page_urls, run_recipe, validate,
)

NOW = datetime(2026, 7, 6, 12, 0)

LISTING = """
<html><body>
  <div class="event"><h3><a href="/e/1">Sommerkonzert</a></h3>
    <span class="date">2026-07-20 19:30</span></div>
  <div class="event"><h3><a href="/e/2">Flohmarkt am Platz</a></h3>
    <span class="date">2026-07-21 09:00</span></div>
  <div class="event"><h3><a href="/e/3">Yoga im Park</a></h3>
    <span class="date">2026-07-22 18:00</span></div>
  <a class="next" href="/events?page=2">weiter</a>
</body></html>
""".encode()

LISTING_P2 = """
<html><body>
  <div class="event"><h3><a href="/e/4">Repair Cafe</a></h3>
    <span class="date">2026-07-25 14:00</span></div>
</body></html>
""".encode()


def _recipe(**kw):
    base = dict(
        entry_urls=["https://x.at/events?page={n}"],
        pagination=Pagination(type="url_param", param="page", start=1, max_pages=2),
        item_scope="div.event",
        field_selectors={"title": "h3 a", "starts_at": ".date", "url": "h3 a@href"},
        validation={"min_items": 2, "required_fields": ["title", "starts_at"]},
    )
    base.update(kw)
    return Recipe(**base)


def test_url_param_expansion():
    urls = page_urls(_recipe(), now=NOW)
    assert urls == ["https://x.at/events?page=1", "https://x.at/events?page=2"]


def test_calendar_nav_expansion_rolls_year():
    r = _recipe(
        entry_urls=["https://x.at/kalender/{year}/{month}"],
        pagination=Pagination(type="calendar_nav", months_ahead=3),
    )
    assert page_urls(r, now=datetime(2026, 11, 15)) == [
        "https://x.at/kalender/2026/11",
        "https://x.at/kalender/2026/12",
        "https://x.at/kalender/2027/01",
    ]


def test_selector_extraction_with_attr_and_base_url():
    payloads = extract_with_selectors(_recipe(), LISTING, "https://x.at/events")
    assert len(payloads) == 3
    assert payloads[0]["title"]["value"] == "Sommerkonzert"
    assert payloads[0]["url"]["value"] == "https://x.at/e/1"
    assert payloads[0]["starts_at"]["value"] == "2026-07-20 19:30"


def test_run_recipe_paginates_and_validates(conn):
    pages = {
        "https://x.at/events?page=1": LISTING,
        "https://x.at/events?page=2": LISTING_P2,
    }
    payloads, result = run_recipe(
        _recipe(), {"id": None}, conn, fetch_page=lambda u: pages.get(u), now=NOW
    )
    assert [p["title"]["value"] for p in payloads] == [
        "Sommerkonzert", "Flohmarkt am Platz", "Yoga im Park", "Repair Cafe",
    ]
    assert result.ok


def test_next_link_following(conn):
    pages = {
        "https://x.at/events": LISTING,
        "https://x.at/events?page=2": LISTING_P2,
    }
    r = _recipe(
        entry_urls=["https://x.at/events"],
        pagination=Pagination(type="next_link", next_selector="a.next@href", max_pages=5),
    )
    payloads, _ = run_recipe(
        r, {"id": None}, conn, fetch_page=lambda u: pages.get(u), now=NOW
    )
    assert len(payloads) == 4  # followed the weiter link


def test_validation_contract_detects_breakage():
    broken = [{"title": {"value": "x", "confidence": 0.9}}]  # no dates at all
    result = validate(_recipe(), broken)
    assert not result.ok
    assert any("min_items" in r for r in result.reasons)
    assert any("starts_at" in r for r in result.reasons)


def test_date_parse_rate_check():
    payloads = [
        {"title": {"value": f"e{i}", "confidence": 0.9},
         "starts_at": {"value": "kein datum", "confidence": 0.9}}
        for i in range(5)
    ]
    result = validate(_recipe(), payloads)
    assert not result.ok
    assert any("date_parse_rate" in r for r in result.reasons)
