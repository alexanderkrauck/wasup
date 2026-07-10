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
    assert _required_horizon(21, 700) == 560  # 80% of what the agent saw
    assert _required_horizon(21, 10_000) == 1460  # 5-year cap
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
