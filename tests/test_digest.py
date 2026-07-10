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
