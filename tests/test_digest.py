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
