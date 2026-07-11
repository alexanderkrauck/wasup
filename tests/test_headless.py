"""render_states against a local replace-style paginator (file://, offline -
real Chromium, no network). This is the capability the linztermine deep
crawl was missing: JSF/PrimeFaces paginators swap the list in place, so
every page state must be harvested before the next click."""

from pathlib import Path

from eventindex.fetch.headless import render_states

FIXTURE = (Path(__file__).parent / "fixtures" / "paginator.html").as_uri()


def test_render_states_harvests_every_page_and_stops_on_disabled():
    states, reason = render_states(FIXTURE, "a#next", max_states=10)
    assert len(states) == 3  # 3 pages, then the disabled control stops the loop
    assert reason == "exhausted"  # natural end, nothing was cut off
    joined = b"\n".join(states)
    for title in (b"<h3>Konzert Alpha</h3>", b"<h3>Markt Gamma</h3>",
                  b"<h3>Kurs Epsilon</h3>"):
        assert title in joined
    # each state is a distinct page, not an accumulated blob (the raw page
    # data lives in the fixture's <script>, hence the rendered-markup check)
    assert b"<h3>Konzert Alpha</h3>" not in states[2]


def test_render_states_without_selector_returns_single_state():
    states, reason = render_states(FIXTURE, None, max_states=10)
    assert len(states) == 1
    assert reason == "exhausted"


def test_render_states_reports_pages_left_behind_at_the_cap():
    """A limit that cuts a productive walk short must never be silent
    (Alexander 2026-07-10) - the caller gets stop_reason 'cap'."""
    states, reason = render_states(FIXTURE, "a#next", max_states=2)
    assert len(states) == 2
    assert reason == "cap"  # page 3 existed and was cut off


def test_render_states_detects_dead_next_control():
    """The prod-linztermine trap: a visible, enabled paginator control whose
    click changes nothing must be reported as 'noop', not mistaken for a
    natural end."""
    states, reason = render_states(FIXTURE, "a#dead", max_states=10)
    assert len(states) == 1
    assert reason == "noop"


def test_deep_probe_reports_last_page_horizon():
    """The horizon check must measure the DEEPEST page the pagination
    reaches, not the chronological page 1 (which shows next week on every
    site regardless of recipe depth)."""
    from eventindex.discovery.onboard import _deep_probe_horizon
    from eventindex.fetch.recipe import Pagination, Recipe

    r = Recipe(
        entry_urls=[FIXTURE],
        pagination=Pagination(type="next_click", next_selector="a#next",
                              max_pages=10),
        item_scope="div.event",
        field_selectors={"title": "h3", "starts_at": ".date"},
    )
    horizon = _deep_probe_horizon(r, {}, None, None)
    # the fixture's last page is dated 2030-10-04 - far beyond any
    # min-horizon requirement, and months beyond its page 1
    assert horizon is not None and horizon > 365
