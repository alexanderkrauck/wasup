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


def test_calendar_expansion_survives_default_max_pages(conn):
    """max_pages (default 10) governs next_link chains - it must never
    truncate the pre-expanded month list (completeness contract)."""
    fetched = []

    def fetch(url):
        fetched.append(url)
        return LISTING_P2

    r = _recipe(
        entry_urls=["https://x.at/kalender/{year}/{month}"],
        pagination=Pagination(type="calendar_nav", months_ahead=12),
        validation={"min_items": 1},
    )
    run_recipe(r, {"id": None}, conn, fetch_page=fetch, now=NOW)
    assert len(fetched) == 12  # all months, not max_pages=10


def test_detail_budget_is_per_crawl(conn, monkeypatch):
    from eventindex.fetch import recipe as recipe_mod

    monkeypatch.setattr(recipe_mod, "MAX_DETAIL_FETCHES", 4)
    detail_fetches = []

    def fetch(url):
        if "/e/" in url:
            detail_fetches.append(url)
            return b"<html>tiny</html>"  # <100 chars text: no LLM call
        return LISTING.replace(b'href="/e/', b'href="/e/%d-' % len(detail_fetches))

    # listing pages use selectors (no LLM); detail pages hit the cascade but
    # are <100 chars of text, which returns [] before any LLM call
    r = _recipe(follow_detail=True, detail_url_selector="h3 a@href")
    run_recipe(r, {"id": None, "kind": "website", "name": "x"}, conn,
               fetch_page=fetch, now=NOW)
    assert len(detail_fetches) == 4  # cap holds across pages, not per page


def test_all_seen_needs_at_least_one_parsed_item():
    from eventindex.fetch.recipe import _all_seen

    garbage = [{"title": {"value": "x", "confidence": 0.9}}]  # no date
    assert _all_seen(garbage, set(), {}) is False  # never stop on garbage


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


def test_date_range_format_and_chunking():
    """{from}/{to} honor the site's date format and split into windows for
    sites that cap results per query (the linztermine lesson)."""
    r = _recipe(
        entry_urls=["https://x.at/suche?from={from}&to={to}"],
        pagination=Pagination(type="date_range_param", months_ahead=2,
                              date_format="%d.%m.%Y", chunk_days=31),
    )
    assert page_urls(r, now=NOW) == [
        "https://x.at/suche?from=06.07.2026&to=05.08.2026",
        "https://x.at/suche?from=06.08.2026&to=05.09.2026",
    ]


def test_date_templates_expand_for_any_pagination_type():
    """Date windows are orthogonal to the pagination mechanics: a next_click
    recipe may start from a date-filtered listing."""
    r = _recipe(
        entry_urls=["https://x.at/suche?from={from}&to="],
        pagination=Pagination(type="next_click", next_selector="a.next",
                              months_ahead=1, date_format="%d.%m.%Y"),
    )
    assert page_urls(r, now=NOW) == ["https://x.at/suche?from=06.07.2026&to="]


def test_interactive_pagination_coerces_headless_render():
    r = _recipe(
        pagination=Pagination(type="next_click", next_selector="a.next"),
    )
    assert r.render == "headless"


def test_next_click_states_are_harvested_per_page(conn):
    """A next_click fetch returns one HTML per page STATE (a replacing
    paginator destroys earlier pages); every state must be extracted."""
    r = _recipe(
        entry_urls=["https://x.at/suche"],
        pagination=Pagination(type="next_click", next_selector="a.next",
                              max_pages=5),
    )
    payloads, result = run_recipe(
        r, {"id": None}, conn, fetch_page=lambda u: [LISTING, LISTING_P2], now=NOW
    )
    assert [p["title"]["value"] for p in payloads] == [
        "Sommerkonzert", "Flohmarkt am Platz", "Yoga im Park", "Repair Cafe",
    ]
    assert result.ok


def test_next_click_states_respect_page_cap(conn):
    r = _recipe(
        entry_urls=["https://x.at/suche"],
        pagination=Pagination(type="next_click", next_selector="a.next",
                              max_pages=1),
        validation={"min_items": 2, "required_fields": ["title", "starts_at"]},
    )
    payloads, _ = run_recipe(
        r, {"id": None}, conn, fetch_page=lambda u: [LISTING, LISTING_P2], now=NOW
    )
    assert len(payloads) == 3  # the state beyond the cap is not extracted


def test_truncation_is_reported_not_silent(conn):
    """Limits that cut a crawl short surface in the validation result
    (Alexander 2026-07-10: a productive source hitting a cap must scream)."""
    r = _recipe(
        entry_urls=["https://x.at/suche"],
        pagination=Pagination(type="next_click", next_selector="a.next",
                              max_pages=1),
        validation={"min_items": 2, "required_fields": ["title", "starts_at"]},
    )
    _, result = run_recipe(
        r, {"id": None}, conn, fetch_page=lambda u: [LISTING, LISTING_P2], now=NOW
    )
    assert result.truncated is not None and "page cap" in result.truncated


def test_unfetched_queued_urls_are_reported(conn):
    r = _recipe(
        entry_urls=[f"https://x.at/events?p={i}" for i in range(1, 6)],
        pagination=Pagination(type="none", max_pages=2),
        validation={"min_items": 2, "required_fields": ["title", "starts_at"]},
    )
    pages = {f"https://x.at/events?p={i}": LISTING for i in range(1, 6)}
    _, result = run_recipe(r, {"id": None}, conn,
                           fetch_page=lambda u: pages.get(u), now=NOW)
    assert result.truncated is None  # entry urls are never silently dropped


def test_llm_wrapped_arrays_are_unwrapped():
    """Some models serialize array tool params as {'item': [...]} - a prod
    session burned 6 emits on exactly this (2026-07-11); coerce it."""
    r = Recipe(
        entry_urls={"items": ["https://x.at/events"]},
        pagination=Pagination(type="none"),
        validation={"min_items": 3,
                    "required_fields": {"item": ["title", "starts_at"]}},
        stop_conditions="date_older_than_now",
    )
    assert r.entry_urls == ["https://x.at/events"]
    assert r.validation.required_fields == ["title", "starts_at"]
    assert r.stop_conditions == ["date_older_than_now"]


def test_nested_llm_wrapped_arrays_are_unwrapped():
    r = Recipe(
        entry_urls=["https://x.at/events"],
        pagination=Pagination(type="none"),
        validation={"required_fields": {"item": {"item": ["title", "starts_at"]}}},
    )
    assert r.validation.required_fields == ["title", "starts_at"]


def test_stop_conditions_skip_to_next_window(conn):
    """date_older_than_now/all_seen end one listing walk, never the whole
    crawl: a chunked-window recipe has more windows in the queue whose
    content the current one says nothing about (2026-07-11)."""
    past = """
    <html><body><div class="event"><h3><a href="/e/9">Alte Messe</a></h3>
    <span class="date">2020-01-01 10:00</span></div>
    <div class="event"><h3><a href="/e/8">Alter Markt</a></h3>
    <span class="date">2020-01-02 10:00</span></div>
    <div class="event"><h3><a href="/e/7">Altes Fest</a></h3>
    <span class="date">2020-01-03 10:00</span></div></body></html>
    """.encode()
    pages = {"https://x.at/w1": past, "https://x.at/w2": LISTING}
    r = _recipe(
        entry_urls=["https://x.at/w1", "https://x.at/w2"],
        pagination=Pagination(type="none"),
        stop_conditions=["date_older_than_now"],
    )
    payloads, _ = run_recipe(r, {"id": None}, conn,
                             fetch_page=lambda u: pages.get(u), now=NOW)
    titles = [p["title"]["value"] for p in payloads]
    assert "Sommerkonzert" in titles  # window 2 was still crawled


def test_next_click_budget_multiplies_windows_by_states(conn):
    """3 window urls x max_pages 3 must allow 9 states - the old max()
    formula capped the whole crawl at 3 and starved the coverage gate's
    measurement (2026-07-11)."""
    r = _recipe(
        entry_urls=[f"https://x.at/w{i}" for i in (1, 2, 3)],
        pagination=Pagination(type="next_click", next_selector="a.next",
                              max_pages=3),
        validation={"min_items": 2, "required_fields": ["title", "starts_at"]},
    )
    payloads, result = run_recipe(
        r, {"id": None}, conn,
        fetch_page=lambda u: [LISTING, LISTING_P2] if u.endswith("w1")
        else [LISTING_P2 if u.endswith("w2") else LISTING],
        now=NOW,
    )
    assert result.pages == 4  # 2 + 1 + 1 states, none dropped by the cap
