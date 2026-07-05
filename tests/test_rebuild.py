"""Rebuild integration: merging, idempotency, status asymmetry, series.

All LLM paths are stubbed - tests must never spend.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from psycopg.types.json import Jsonb

from eventindex.resolve import rebuild as rb
from eventindex.resolve import recurrence

NOW = datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.setattr(rb.llm, "complete", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("LLM called in test")
    ))
    monkeypatch.setattr(recurrence, "verify", lambda *a, **k: True)
    # file writes escape the test transaction
    monkeypatch.setattr(rb, "_dump_venue_review", lambda *a, **k: None)


def _source(conn, name, trust, lat=48.31, lon=14.29):
    return conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, geo) VALUES "
        "(%s, %s, 'website', 2, %s, ST_SetSRID(ST_MakePoint(%s, %s), 4326)) "
        "RETURNING id",
        (name, f"https://{name}.at", trust, lon, lat),
    ).fetchone()["id"]


def _claim(conn, source_id, fields, fingerprint, extracted_at=NOW):
    payload = {k: {"value": v, "confidence": c} for k, (v, c) in fields.items()}
    conn.execute(
        "INSERT INTO event_claim (source_id, fingerprint, payload, extracted_at) "
        "VALUES (%s, %s, %s, %s)",
        (source_id, fingerprint, Jsonb(payload), extracted_at),
    )


def _concert(title, starts="2026-07-20T20:00:00+02:00", venue="Posthof", **extra):
    fields = {
        "title": (title, 0.95), "starts_at": (starts, 0.95),
        "venue_name": (venue, 0.9),
    }
    fields.update(extra)
    return fields


def _canon(conn):
    events = conn.execute(
        "SELECT id, title, kind, confidence, field_provenance, venue_id "
        "FROM event ORDER BY title"
    ).fetchall()
    occs = conn.execute(
        "SELECT id, event_id, starts_at, status FROM occurrence ORDER BY starts_at, id"
    ).fetchall()
    return events, occs


def test_three_sources_one_event_with_compound_confidence(conn):
    ids = [_source(conn, f"s{i}", 0.8) for i in range(3)]
    for i, sid in enumerate(ids):
        _claim(conn, sid, _concert("Chet Faker"), f"chet faker|2026-07-20|cell{i}")
    rb.rebuild(conn, now=NOW)

    events, occs = _canon(conn)
    assert len(events) == 1
    assert len(occs) == 1
    event = events[0]
    assert event["title"] == "Chet Faker"
    # 3 × 0.8-trust sources compound beyond any single one (§7)
    assert event["confidence"] > 0.9
    claims = conn.execute("SELECT count(DISTINCT source_id) AS n FROM event_claim").fetchone()
    assert claims["n"] == 3


def test_rebuild_is_idempotent(conn):
    ids = [_source(conn, f"s{i}", 0.8) for i in range(2)]
    _claim(conn, ids[0], _concert("Konzert A"), "konzert a|2026-07-20|x")
    _claim(conn, ids[1], _concert("Flohmarkt", starts="2026-07-21T09:00:00+02:00",
                                  venue="Hauptplatz"), "flohmarkt|2026-07-21|y")
    rb.rebuild(conn, now=NOW)
    first = _canon(conn)
    rb.rebuild(conn, now=NOW)
    assert _canon(conn) == first


def test_identity_survives_new_claims(conn):
    sid = _source(conn, "s0", 0.8)
    _claim(conn, sid, _concert("Konzert A"), "konzert a|2026-07-20|x")
    rb.rebuild(conn, now=NOW)
    event_id = _canon(conn)[0][0]["id"]

    _claim(conn, sid, _concert("Konzert A", starts="2026-07-20T20:30:00+02:00"),
           "konzert a|2026-07-20|x", extracted_at=NOW + timedelta(days=1))
    rb.rebuild(conn, now=NOW)
    assert _canon(conn)[0][0]["id"] == event_id


def test_trusted_negative_beats_older_positives(conn):
    portal_a = _source(conn, "portal-a", 0.9)
    portal_b = _source(conn, "portal-b", 0.9)
    venue = _source(conn, "venue-site", 0.8)
    fields = _concert("Sommerfest")
    _claim(conn, portal_a, fields, "sommerfest|2026-07-20|a", NOW - timedelta(days=2))
    _claim(conn, portal_b, fields, "sommerfest|2026-07-20|b", NOW - timedelta(days=2))
    cancelled = _concert("Sommerfest", status=("cancelled", 0.9))
    _claim(conn, venue, cancelled, "sommerfest|2026-07-20|c", NOW - timedelta(hours=1))
    rb.rebuild(conn, now=NOW)

    _, occs = _canon(conn)
    assert [o["status"] for o in occs] == ["cancelled"]
    # confirmation sweep: the event's other sources get re-crawled
    sweeps = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE kind = 'crawl' AND status = 'pending'"
    ).fetchone()
    assert sweeps["n"] == 3


def test_newer_higher_trust_positive_reverts_negative(conn):
    venue = _source(conn, "venue-site", 0.8)
    portal = _source(conn, "portal", 0.9)
    _claim(conn, venue, _concert("Sommerfest", status=("cancelled", 0.9)),
           "sommerfest|2026-07-20|a", NOW - timedelta(days=1))
    _claim(conn, portal, _concert("Sommerfest"),
           "sommerfest|2026-07-20|b", NOW - timedelta(hours=1))
    rb.rebuild(conn, now=NOW)
    _, occs = _canon(conn)
    assert [o["status"] for o in occs] == ["scheduled"]


def test_explicit_multidate_becomes_one_series(conn):
    sid = _source(conn, "portal", 0.9)
    for day in (20, 21, 27):
        _claim(conn, sid, _concert("Grünmarkt", starts=f"2026-07-{day}T06:00:00+02:00",
                                   venue="Hauptplatz"),
               f"gruenmarkt|2026-07-{day}|x")
    rb.rebuild(conn, now=NOW)
    events, occs = _canon(conn)
    assert len(events) == 1
    assert events[0]["kind"] == "series"
    assert len(occs) == 3


def test_recurrence_claim_expands_and_skips_summer_holidays(conn):
    sid = _source(conn, "gym", 0.8)
    rec = {
        "freq": "weekly", "weekday": "TU", "week_of_month": None, "interval": 1,
        "time": "18:30", "duration_minutes": 60,
        "except_holidays": ["school_holidays"],
        "valid_from": "2026-09-01", "valid_until": None,
        "as_stated": "jeden Dienstag 18:30, außer in den Schulferien",
    }
    fields = _concert("Spinning", starts="2026-09-15T18:30:00+02:00", venue="Gym X",
                      recurrence=(rec, 0.8))
    _claim(conn, sid, fields, "spinning|2026-09-15|x")
    september_now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    rb.rebuild(conn, now=september_now)

    events, occs = _canon(conn)
    assert events[0]["kind"] == "series"
    days = [o["starts_at"].astimezone(timezone.utc).date().isoformat() for o in occs]
    assert "2026-09-08" not in days  # Sommerferien until Sep 13
    assert any(d == "2026-09-15" for d in days)
