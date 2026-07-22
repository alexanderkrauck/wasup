"""Value checkpoint (2026-07-08): the agent's yield estimate may extend the
session rings, but only through the deterministic gate and never past the
hard rings. LLM stubbed - tests must never spend."""

from types import SimpleNamespace

from eventindex import config
from eventindex.discovery import onboard


def test_extended_rings_scale_and_clamp():
    # small yield: base rings, no extension
    cap, turns, wall = onboard._extended_rings(10)
    assert (cap, turns, wall) == (config.ONBOARD_SESSION_CAP_EUR,
                                  config.ONBOARD_MAX_TURNS,
                                  config.ONBOARD_WALL_CLOCK_S)
    # big yield: everything clamps to the hard rings
    cap, turns, wall = onboard._extended_rings(500)
    assert cap == config.ONBOARD_HARD_CAP_EUR
    assert turns == config.ONBOARD_HARD_MAX_TURNS
    assert wall == config.ONBOARD_HARD_WALL_CLOCK_S
    # mid yield: proportional, monotonic between the rings
    cap, turns, wall = onboard._extended_rings(40)  # 40 * 0.03 = 1.20
    assert config.ONBOARD_SESSION_CAP_EUR < cap < config.ONBOARD_HARD_CAP_EUR
    assert config.ONBOARD_MAX_TURNS < turns <= config.ONBOARD_HARD_MAX_TURNS


def _checkpoint_with(monkeypatch, reply: str):
    monkeypatch.setattr(
        onboard.llm, "chat",
        lambda tx, messages, **kw: SimpleNamespace(content=reply),
    )
    return onboard._value_checkpoint(
        None, [], onboard.Session(), "mini", {"id": None}, None
    )


def test_checkpoint_extends_on_credible_estimate(monkeypatch):
    (cap, turns, wall), expected = _checkpoint_with(monkeypatch, (
        '{"expected_events_per_crawl": 100, "expects_success": true, '
        '"rationale": "large calendar"}'
    ))
    assert cap == config.ONBOARD_HARD_CAP_EUR
    assert expected == 100  # the estimate feeds the coverage gate


def test_checkpoint_fails_closed_on_garbage(monkeypatch):
    (cap, turns, wall), expected = _checkpoint_with(
        monkeypatch, "I think it looks promising!")
    assert (cap, turns, wall) == (config.ONBOARD_SESSION_CAP_EUR,
                                  config.ONBOARD_MAX_TURNS,
                                  config.ONBOARD_WALL_CLOCK_S)
    assert expected is None


def test_checkpoint_keeps_base_when_agent_expects_failure(monkeypatch):
    (cap, _, _), expected = _checkpoint_with(monkeypatch, (
        '{"expected_events_per_crawl": 100, "expects_success": false, '
        '"rationale": "login wall"}'
    ))
    assert cap == config.ONBOARD_SESSION_CAP_EUR
    assert expected is None


def test_zero_items_hint_blames_params_when_content_is_reachable():
    """The old blanket 'needs headless' hint misdiagnosed linztermine (its
    HTML is server-rendered; the URL params were wrong) and sent retries
    into a dead end."""
    from eventindex.discovery.onboard import _diagnose_zero_items

    assert "parameters or pagination" in _diagnose_zero_items("http", True)
    assert "parameters or pagination" in _diagnose_zero_items("headless", True)


def test_zero_items_hint_suggests_headless_only_without_http_content():
    from eventindex.discovery.onboard import _diagnose_zero_items

    assert "headless" in _diagnose_zero_items("http", False)
    assert "entry URL" in _diagnose_zero_items("headless", False)


def test_page_count_extrapolates_next_click_depth():
    from eventindex.discovery.onboard import _page_count
    from eventindex.fetch.recipe import Pagination, Recipe

    flat = Recipe(entry_urls=["https://x.at/suche?from={from}&to={to}"],
                  pagination=Pagination(type="date_range_param", months_ahead=3,
                                        chunk_days=31))
    assert _page_count(flat) == 3  # one page per window
    deep = Recipe(entry_urls=["https://x.at/suche?from={from}&to={to}"],
                  pagination=Pagination(type="next_click", next_selector="a.n",
                                        months_ahead=3, chunk_days=31,
                                        max_pages=20))
    assert _page_count(deep) == 60  # 3 windows x 20 states


def test_coverage_gate_rejects_first_page_only_recipes(conn, monkeypatch):
    """A recipe that validates at 15 items/window while the agent itself
    estimated ~1000 events is the 'valid but 4%-of-the-site' failure mode
    (linztermine attempt 2) - it must bounce back to the agent."""
    from eventindex.discovery import onboard as ob
    from eventindex.fetch.recipe import Pagination, Recipe, ValidationResult

    r = Recipe(entry_urls=["https://x.at/suche?from={from}&to={to}"],
               pagination=Pagination(type="date_range_param", months_ahead=3,
                                     chunk_days=31))
    fake_payloads = [{"title": {"value": f"E{i}"},
                      "starts_at": {"value": "2030-01-01 10:00"}} for i in range(15)]
    monkeypatch.setattr(ob, "run_recipe", lambda *a, **k: (
        fake_payloads, ValidationResult(ok=True, items=15, reasons=[])))
    _, error = ob._self_validate(r, ["E1"], {"id": None}, conn, None,
                                 expected_events=1000)
    assert error is not None and "COVERAGE TOO LOW" in error
    # with a matching estimate the same recipe passes
    payloads, error = ob._self_validate(r, ["E1"], {"id": None}, conn, None,
                                        expected_events=40)
    assert error is None


def test_required_horizon_scales_with_reported_site_horizon():
    from eventindex.discovery.onboard import _required_horizon

    assert _required_horizon(21, None) == 21
    assert abs(_required_horizon(21, 700) - 490) < 0.01  # 70% of what the agent saw
    assert abs(_required_horizon(21, 10_000) - 1277.5) < 0.01  # 5-year cap
    assert _required_horizon(None, None) == 0


def test_horizon_gate_rejects_recipes_short_of_site_horizon(conn, monkeypatch):
    """'Alle Events, die es gibt' (Alexander 2026-07-10): a recipe covering
    3 months of a site that publishes 2 years ahead must bounce, even if it
    clears the static 21-day minimum."""
    from eventindex.discovery import onboard as ob
    from eventindex.fetch.recipe import Pagination, Recipe, ValidationResult

    r = Recipe(entry_urls=["https://x.at/suche?from={from}&to={to}"],
               pagination=Pagination(type="date_range_param", months_ahead=3,
                                     chunk_days=31))
    payloads = [{"title": {"value": "E"}, "starts_at": {"value": "2026-08-01"}}] * 5
    monkeypatch.setattr(ob, "run_recipe", lambda *a, **k: (
        payloads, ValidationResult(ok=True, items=5, reasons=[])))
    monkeypatch.setattr(ob, "_deep_probe_horizon", lambda *a, **k: 90.0)
    _, error = ob._self_validate(r, ["E"], {"id": None}, conn, None,
                                 min_horizon_days=21, site_horizon_days=700)
    assert error is not None and "HORIZON TOO SHALLOW" in error
    # a deep probe that reaches the site's horizon passes
    monkeypatch.setattr(ob, "_deep_probe_horizon", lambda *a, **k: 600.0)
    _, error = ob._self_validate(r, ["E"], {"id": None}, conn, None,
                                 min_horizon_days=21, site_horizon_days=700)
    assert error is None


def test_median_horizon_resists_single_far_outlier():
    """One long-running exhibition on page 1 must not impersonate recipe
    depth (prod linztermine shipped a 15-event recipe past the max-based
    horizon check, 2026-07-10)."""
    from eventindex.discovery.onboard import _median_horizon_days

    near = [{"starts_at": {"value": "2026-07-12 10:00"}} for _ in range(9)]
    outlier = [{"starts_at": {"value": "2028-01-01 10:00"}}]
    horizon = _median_horizon_days(near + outlier)
    assert horizon is not None and horizon < 10


def test_coverage_gate_trusts_measurement_over_plan(conn, monkeypatch):
    """A next_click recipe planning 80 pages but measuring 1 fetched state
    (dead selector) must not pass on plan-based extrapolation."""
    from eventindex.discovery import onboard as ob
    from eventindex.fetch.recipe import Pagination, Recipe, ValidationResult

    r = Recipe(entry_urls=["https://x.at/suche?from={from}&to="],
               pagination=Pagination(type="next_click", next_selector="a.dead",
                                     max_pages=80))
    payloads = [{"title": {"value": f"E{i}"},
                 "starts_at": {"value": "2030-01-01 10:00"}} for i in range(15)]

    def fake_run(trimmed, *a, **k):
        return payloads, ValidationResult(
            ok=True, items=15, reasons=[], pages=1,
            pagination_noop="clicking 'a.dead' changed nothing")

    monkeypatch.setattr(ob, "run_recipe", fake_run)
    _, error = ob._self_validate(r, ["E1"], {"id": None}, conn, None,
                                 expected_events=1100)
    assert error is not None and "COVERAGE TOO LOW" in error
    assert "DEAD control" in error

    def fake_run_ok(trimmed, *a, **k):
        return payloads, ValidationResult(ok=True, items=15, reasons=[], pages=3)

    monkeypatch.setattr(ob, "run_recipe", fake_run_ok)
    _, error = ob._self_validate(r, ["E1"], {"id": None}, conn, None,
                                 expected_events=1100)
    assert error is None  # fully-measured pagination extrapolates honestly


def test_validation_ignores_stop_conditions(conn, monkeypatch):
    """all_fingerprints_seen halted a validation crawl on page 1 (the old
    broken recipe had claimed exactly that page daily) and the coverage
    gate read '1 page fetched' for a working recipe (2026-07-11)."""
    from eventindex.discovery import onboard as ob
    from eventindex.fetch.recipe import Pagination, Recipe, ValidationResult

    seen = {}

    def fake_run(trimmed, *a, **k):
        seen["stops"] = list(trimmed.stop_conditions)
        return ([{"title": {"value": "E"}, "starts_at": {"value": "2030-01-01"}}] * 5,
                ValidationResult(ok=True, items=5, reasons=[], pages=3))

    monkeypatch.setattr(ob, "run_recipe", fake_run)
    r = Recipe(entry_urls=["https://x.at/e"], pagination=Pagination(type="none"),
               stop_conditions=["all_fingerprints_seen", "date_older_than_now"])
    ob._self_validate(r, ["E"], {"id": None}, conn, None)
    assert seen["stops"] == []
    assert r.stop_conditions == ["all_fingerprints_seen", "date_older_than_now"]


def test_validation_clamps_expanded_urls(conn, monkeypatch):
    """A chunk_days=2 template expands to ~365 window urls; validation must
    walk at most 3 concrete pages, not the whole expansion (a validation
    crawl ran >1h inside one agent turn, 2026-07-11)."""
    from eventindex.discovery import onboard as ob
    from eventindex.fetch.recipe import Pagination, Recipe, ValidationResult, page_urls

    seen = {}

    def fake_run(trimmed, *a, **k):
        seen["urls"] = len(page_urls(trimmed))
        return ([{"title": {"value": "E"}, "starts_at": {"value": "2030-01-01"}}] * 5,
                ValidationResult(ok=True, items=5, reasons=[], pages=3))

    monkeypatch.setattr(ob, "run_recipe", fake_run)
    r = Recipe(entry_urls=["https://x.at/suche?from={from}&to={to}"],
               pagination=Pagination(type="date_range_param", months_ahead=24,
                                     chunk_days=2))
    ob._self_validate(r, ["E"], {"id": None}, conn, None)
    assert seen["urls"] == 3


def test_venue_gate_bounces_locationless_recipes(conn, monkeypatch):
    """Venue contract (2026-07-14): an escalated onboard must not accept a
    recipe that yields neither venues nor per-item detail URLs (the WKO
    recipe shipped exactly that: title+date off the listing, everything
    else on detail pages it never followed)."""
    from eventindex.discovery import onboard as ob
    from eventindex.fetch.recipe import Pagination, Recipe, ValidationResult

    r = Recipe(entry_urls=["https://x.at/events"], pagination=Pagination(type="none"))
    bare = [{"title": {"value": f"E{i}"},
             "starts_at": {"value": "2030-01-01 10:00"}} for i in range(6)]
    monkeypatch.setattr(ob, "run_recipe", lambda *a, **k: (
        bare, ValidationResult(ok=True, items=6, reasons=[])))
    _, error = ob._self_validate(r, ["E1"], {"id": None}, conn, None,
                                 require_venues=True)
    assert error is not None and "DETAIL URLS MISSING" in error

    with_urls = [dict(p, url={"value": f"https://x.at/e/{i}"})
                 for i, p in enumerate(bare)]
    monkeypatch.setattr(ob, "run_recipe", lambda *a, **k: (
        with_urls, ValidationResult(ok=True, items=6, reasons=[])))
    _, error = ob._self_validate(r, ["E1"], {"id": None}, conn, None,
                                 require_venues=True)
    assert error is not None and "VENUES MISSING" in error

    with_venues = [dict(p, url={"value": f"https://x.at/e/{i}"},
                        venue_name={"value": f"Venue {i}"})
                   for i, p in enumerate(bare)]
    monkeypatch.setattr(ob, "run_recipe", lambda *a, **k: (
        with_venues, ValidationResult(ok=True, items=6, reasons=[])))
    _, error = ob._self_validate(r, ["E1"], {"id": None}, conn, None,
                                 require_venues=True)
    assert error is None

    # follow_detail=true is the accepted answer when the listing lacks venues
    r_deep = Recipe(entry_urls=["https://x.at/events"],
                    pagination=Pagination(type="none"), follow_detail=True)
    _, error = ob._self_validate(r_deep, ["E1"], {"id": None}, conn, None,
                                 require_venues=True)
    assert error is None
    # without the escalation flag the same bare recipe still passes (soft path)
    monkeypatch.setattr(ob, "run_recipe", lambda *a, **k: (
        bare, ValidationResult(ok=True, items=6, reasons=[])))
    _, error = ob._self_validate(r, ["E1"], {"id": None}, conn, None)
    assert error is None


def test_failure_notes_distill_checkpoint_emit_and_apis():
    session = onboard.Session()
    session.record("navigate", {"url": "https://x.at"}, "shell")
    session.record("value_checkpoint", {},
                   "expected=17 success=True | Nexudus JSON API returns all events")
    session.record("emit_recipe", {},
                   "SELF-VALIDATION FAILED: interpreter validation failed: items 0")
    browser = onboard.Browser()
    browser._api_calls = {"https://x.spaces.nexudus.com/api/public/events": 200}
    notes = onboard._failure_notes(session, browser)
    assert "Nexudus JSON API" in notes
    assert "items 0" in notes
    assert "api/public/events" in notes


def test_prior_notes_enter_the_prompt_and_failures_carry_notes(monkeypatch):
    prompts = {}

    def fake_chat(tx, messages, **kw):
        prompts["user"] = messages[1]["content"]
        return SimpleNamespace(content="", tool_calls=None)

    monkeypatch.setattr(onboard.llm, "chat", fake_chat)
    monkeypatch.setattr(onboard.config, "ONBOARD_MAX_TURNS", 2)
    monkeypatch.setattr(onboard, "_http_text_len", lambda url: 5000)
    monkeypatch.setattr(onboard, "_spent_on_job", lambda tx, job_id: 0.0)
    source = {"id": None, "url": "https://x.at", "name": "X", "recipe": None,
              "recipe_version": 0,
              "extraction_hint": {"onboard_notes": ["use the nexudus json api"],
                                  "probe_score": 1.0}}
    try:
        onboard.onboard_source(None, source, None, "mini")
        raised = None
    except onboard.OnboardFailed as e:
        raised = e
    assert raised is not None
    assert "PREVIOUS ATTEMPTS LEARNED" in prompts["user"]
    assert "use the nexudus json api" in prompts["user"]
    # the raw notes key must not double-inject through Known hints
    assert '"onboard_notes"' not in prompts["user"]
