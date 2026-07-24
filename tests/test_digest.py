from datetime import datetime, timedelta, timezone

from eventindex.jobs.digest import render

NOW = datetime(2026, 7, 3, 22, 0, tzinfo=timezone.utc)


def _stats(last_success):
    return {
        "crawls": [{"status": "ok", "n": 3, "events": 7}],
        "spend": [{"category": "llm", "eur": 0.1234, "n": 5}],
        "failed_jobs": [],
        "last_success": last_success,
    }


def test_no_dead_man_when_fresh():
    text = render(_stats(NOW - timedelta(hours=2)), NOW)
    assert "DEAD MAN" not in text
    assert "ok: 3 (events found: 7)" in text
    assert "llm: €0.1234 over 5 calls" in text


def test_dead_man_when_stale():
    text = render(_stats(NOW - timedelta(hours=49)), NOW)
    assert "DEAD MAN'S SWITCH" in text


def test_dead_man_when_never_crawled():
    text = render(_stats(None), NOW)
    assert "DEAD MAN'S SWITCH" in text
    assert "last: never" in text


def test_qa_section_renders_check_results():
    stats = _stats(NOW) | {
        "qa": [{"detail": "qa: checked=20 confirmed=18 cancelled=1 not_found=1"}]
    }
    text = render(stats, NOW)
    assert "qa: checked=20 confirmed=18" in text


def test_qa_section_flags_silence():
    assert "QA loop did not run" in render(_stats(NOW), NOW)


def test_limit_warning_screams_when_productive_source_truncated():
    stats = _stats(NOW - timedelta(hours=2))
    stats["limits_hit"] = [{"name": "linztermine (site, deep)",
                            "events_found": 426,
                            "detail": "method=recipe v2 LIMIT-TRUNCATED: state cap 100 hit"}]
    stats["budget_parked"] = [{"name": "big portal", "yield_ema": 300.0,
                               "last_error": "source monthly budget - waiting"}]
    text = render(stats, NOW)
    assert "EVENTS ARE BEING MISSED" in text
    assert "linztermine (site, deep)" in text
    assert "big portal" in text


def test_no_limit_warning_without_hits():
    text = render(_stats(NOW - timedelta(hours=2)), NOW)
    assert "EVENTS ARE BEING MISSED" not in text


def test_day_curve_anomaly_flags_capped_feed_signature():
    from datetime import date

    from eventindex.jobs.digest import day_curve_anomalies

    # three Wednesdays at ~20 events, the fourth collapses to 3: the
    # signature of a feed horizon ending mid-window
    curve = [{"day": date(2026, 7, 15), "n": 20},
             {"day": date(2026, 7, 22), "n": 21},
             {"day": date(2026, 7, 29), "n": 19},
             {"day": date(2026, 8, 5), "n": 3}]
    flags = day_curve_anomalies(curve)
    assert len(flags) == 1 and "2026-08-05" in flags[0]
    # low-volume weekdays never alert (median gate)
    quiet = [{"day": date(2026, 7, 13), "n": 2}, {"day": date(2026, 7, 20), "n": 0}]
    assert day_curve_anomalies(quiet) == []


def test_credit_outage_and_low_balance_scream():
    stats = _stats(NOW) | {
        "credit_parked": {"n": 41, "resume": NOW + timedelta(hours=1)},
        "openrouter_balance_usd": 3.5,
    }
    text = render(stats, NOW)
    assert "LLM CREDITS EMPTY: 41 jobs paused" in text
    assert "OPENROUTER BALANCE LOW: $3.50" in text


def test_healthy_balance_stays_quiet():
    stats = _stats(NOW) | {
        "credit_parked": {"n": 0, "resume": None},
        "openrouter_balance_usd": 80.0,
    }
    text = render(stats, NOW)
    assert "CREDITS EMPTY" not in text
    assert "BALANCE LOW" not in text


def test_fetch_blocked_suspects_render():
    stats = _stats(NOW) | {"fetch_blocked": [{"name": "Stadionwelt"}]}
    text = render(stats, NOW)
    assert "FETCH-BLOCKED SUSPECTS" in text
    assert "Stadionwelt" in text


def test_field_completeness_and_hydration_render():
    stats = _stats(NOW) | {
        "field_completeness": {
            "future_events": 100,
            "stated_price": 25,
            "any_price": 90,
            "booking_without_stated_price": 7,
            "event_scale": 95,
        },
        "hydration": {
            "unresolved": 8,
            "oldest_unresolved": NOW - timedelta(hours=6),
            "failed_24h": 2,
        },
    }
    text = render(stats, NOW)
    assert "stated price: 25/100 (25.0%)" in text
    assert "any price (stated or estimated): 90/100 (90.0%)" in text
    assert "event scale estimate: 95/100 (95.0%)" in text
    assert "booking URL without stated price: 7" in text
    assert "hydration jobs: 8 unresolved, oldest 6:00:00 ago" in text
