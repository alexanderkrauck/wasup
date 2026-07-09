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
    monkeypatch.setattr(rb, "_dump_review", lambda *a, **k: None)


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


def test_weekly_implicit_series_projects_beyond_feed_horizon(conn):
    """Completeness contract: a 7-day-capped feed showing a weekly series
    gets projected forward - but only past what the feed could have shown."""
    sid = _source(conn, "capped-feed", 0.9)
    conn.execute(
        "UPDATE source SET extraction_hint = '{\"horizon_days\": 7}' WHERE id = %s",
        (sid,),
    )
    for day in ("06-10", "06-17", "06-24", "07-01"):  # Wednesdays
        _claim(conn, sid, _concert("Zumba im Park",
                                   starts=f"2026-{day}T18:00:00+02:00",
                                   venue="Donaupark"),
               f"zumba|2026-{day}|x")
    rb.rebuild(conn, now=NOW)  # NOW = Jul 5; coverage edge = Jul 5 + 7d = Jul 12

    events, _ = _canon(conn)
    assert len(events) == 1 and events[0]["kind"] == "series"
    rows = conn.execute(
        "SELECT starts_at, projected FROM occurrence ORDER BY starts_at"
    ).fetchall()
    projected = [
        r["starts_at"].astimezone(timezone.utc).date().isoformat()
        for r in rows if r["projected"]
    ]
    # Jul 8 is inside the demonstrated feed reach -> absence is evidence;
    # Jul 15/22/29 are beyond it -> projected (cap: last observed + 4 weeks)
    assert projected == ["2026-07-15", "2026-07-22", "2026-07-29"]
    assert len(rows) == 7  # 4 observed + 3 projected

    # a deeper crawl proves the feed reaches past the projections without
    # showing the event again -> the projections die at the next rebuild
    conn.execute(
        "UPDATE source SET extraction_hint = '{\"horizon_days\": 60}' WHERE id = %s",
        (sid,),
    )
    rb.rebuild(conn, now=NOW)
    left = conn.execute(
        "SELECT count(*) AS n FROM occurrence WHERE projected"
    ).fetchone()
    assert left["n"] == 0


def test_irregular_dates_are_never_projected(conn):
    sid = _source(conn, "portal", 0.9)
    for day in ("06-10", "06-14", "06-29"):
        _claim(conn, sid, _concert("Lesung", starts=f"2026-{day}T19:00:00+02:00",
                                   venue="Stifterhaus"),
               f"lesung|2026-{day}|x")
    rb.rebuild(conn, now=NOW)
    n = conn.execute("SELECT count(*) AS n FROM occurrence WHERE projected").fetchone()
    assert n["n"] == 0


def test_text_recurrence_regex_gate():
    assert rb._TEXT_REC_RE.search("Salsa - jeden Dienstag um 19:00")
    assert rb._TEXT_REC_RE.search("Treff montags im Vereinsheim")
    assert rb._TEXT_REC_RE.search("wöchentlich, 3.6. bis 26.8.")
    assert not rb._TEXT_REC_RE.search("Konzert am 20. Juli im Posthof")


def test_cached_text_recurrence_makes_a_series_without_llm(conn):
    import hashlib

    sid = _source(conn, "aggregator", 0.9)
    desc = "Salsa Social - jeden Dienstag um 19:00 im Club X"
    key = hashlib.md5(f"textrec|{desc}".encode()).hexdigest()
    rec = {
        "freq": "weekly", "weekday": "TU", "week_of_month": None, "interval": 1,
        "time": "19:00", "duration_minutes": None, "except_holidays": [],
        "valid_from": None, "valid_until": None,
        "as_stated": "jeden Dienstag um 19:00",
    }
    conn.execute(
        "INSERT INTO text_recurrence (content_key, recurrence) VALUES (%s, %s)",
        (key, Jsonb(rec)),
    )
    _claim(conn, sid, _concert("Salsa Social", starts="2026-07-07T19:00:00+02:00",
                               venue="Club X", description=(desc, 0.9)),
           "salsa|2026-07-07|x")
    rb.rebuild(conn, now=NOW)  # no_llm active: only the cache may answer

    events, occs = _canon(conn)
    assert events[0]["kind"] == "series"
    assert len(occs) > 4  # expanded over the 8-week horizon


def test_adjudicated_merge_aliases_nearby_venue(conn, monkeypatch):
    monkeypatch.setattr(
        rb.llm, "complete", lambda tx, prompt, schema, **kw: schema(same_event=True)
    )
    a = _source(conn, "festival-site", 0.9)
    b = _source(conn, "venue-site", 0.8)
    salt = uuid.uuid4().hex[:6]
    _claim(conn, a, {
        "title": ("Ahoi Pop: Bilderbuch", 0.9),
        "starts_at": ("2026-07-20T20:00:00+02:00", 0.9),
        "venue_name": ("Donaupark Bühne", 0.9),
        "lat": (48.310, 0.9), "lon": (14.290, 0.9),
    }, f"ahoi bilderbuch|2026-07-20|a{salt}")
    _claim(conn, b, {
        "title": ("Bilderbuch", 0.9),
        "starts_at": ("2026-07-20T20:00:00+02:00", 0.9),
        "venue_name": ("Posthof Aussenbereich", 0.9),
        "lat": (48.311, 0.9), "lon": (14.291, 0.9),
    }, f"bilderbuch|2026-07-20|b{salt}")
    rb.rebuild(conn, now=NOW)

    events, occs = _canon(conn)
    assert len(events) == 1  # adjudicator merged the marquee pair
    aliased = conn.execute(
        "SELECT name, aliases FROM venue "
        "WHERE 'Posthof Aussenbereich' = ANY(aliases) OR "
        "      'Donaupark Bühne' = ANY(aliases)"
    ).fetchone()
    assert aliased is not None  # <300m apart -> alias, not review


def test_high_score_different_verdict_gets_mid_second_opinion(conn, monkeypatch):
    from eventindex import config

    responses = [False, True]
    models = []

    def fake_complete(tx, prompt, schema, model=None, **kw):
        models.append(model)
        return schema(same_event=responses.pop(0))

    monkeypatch.setattr(rb.llm, "complete", fake_complete)
    a = _source(conn, "portal-x", 0.9)
    b = _source(conn, "portal-y", 0.9)
    # unique fingerprints per run: adjudication verdicts persist across test
    # runs (own-connection writes), a reused pair would hit the cache
    salt = uuid.uuid4().hex[:6]
    _claim(conn, a, _concert("Tom Jones", venue="Domplatz"),
           f"tom jones|2026-07-20|a{salt}")
    _claim(conn, b, _concert("Tom Jones", venue="Musikpavillon Urfahr"),
           f"tom jones|2026-07-20|b{salt}")
    rb.rebuild(conn, now=NOW)

    assert models == [None, config.MODEL_MID]  # mini said no -> mid re-asked
    events, _ = _canon(conn)
    assert len(events) == 1
    verdict = conn.execute(
        "SELECT decided_by, same_event FROM adjudication "
        "WHERE title_a = 'Tom Jones' AND decided_by = 'llm_mid'"
    ).fetchone()
    assert verdict and verdict["same_event"] is True


def test_private_intent_event_publishes_without_geo(conn):
    sid = _source(conn, "social-like", 0.7)
    _claim(conn, sid, {
        "title": ("Gartenfest bei Maria", 0.9),
        "starts_at": ("2026-07-20T15:00:00+02:00", 0.9),
        "address": ("Wildbergstraße 18, 4040 Linz", 0.8),
        "organizer": ("Maria Huber", 0.8),
    }, "gartenfest|2026-07-20|x")
    rb.rebuild(conn, now=NOW)

    row = conn.execute(
        "SELECT title, geo IS NULL AS suppressed FROM event"
    ).fetchone()
    assert row["title"] == "Gartenfest bei Maria"  # listed, not hidden
    assert row["suppressed"]  # but the residential location is withheld


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


def test_text_recurrence_verified_at_birth(conn, monkeypatch):
    """A text-extracted rule the verifier rejects (wrong weekday) must never
    become a series - the claim stays a one-off and the rejection is cached."""
    import hashlib

    from eventindex.resolve.recurrence import Recurrence

    bad_rec = Recurrence(
        freq="weekly", weekday="MO", week_of_month=None, interval=1,
        time="18:00", duration_minutes=None, except_holidays=[],
        valid_from=None, valid_until=None, as_stated="jeden Mittwoch 18:00",
    )
    monkeypatch.setattr(rb.llm, "complete", lambda *a, **k: bad_rec)
    monkeypatch.setattr(recurrence, "verify", lambda *a, **k: False)  # net says no

    sid = _source(conn, "runclub", 0.8)
    desc = "Tempo-Einheit, jeden Mittwoch 18:00 am OK Platz"
    _claim(conn, sid, _concert("Wednesday Tempo",
                               starts="2026-07-08T18:00:00+02:00",
                               venue="OK Platz", description=(desc, 0.9)),
           "wednesday tempo|2026-07-08|x")
    rb.rebuild(conn, now=NOW)

    events, occs = _canon(conn)
    assert events[0]["kind"] == "one_off"  # not a wrong-weekday series
    assert len(occs) == 1                  # observed date only
    key = hashlib.md5(f"textrec|{desc}".encode()).hexdigest()
    cached = conn.execute(
        "SELECT recurrence FROM text_recurrence WHERE content_key = %s", (key,)
    ).fetchone()
    assert cached is not None and cached["recurrence"] is None  # rejected for good


def test_source_fallback_geo_is_never_published(conn):
    """Blocking may use the aggregator's own point; the API must not - three
    unrelated events at identical 'downtown' coords is silent wrong data."""
    sid = _source(conn, "aggregator-geo", 0.9)  # source HAS geo
    _claim(conn, sid, {"title": ("Geheimnisvolle Lesung", 0.9),
                       "starts_at": ("2026-07-20T19:00:00+02:00", 0.9)},
           "lesung geo|2026-07-20|x")  # no venue, no coords
    rb.rebuild(conn, now=NOW)
    row = conn.execute(
        "SELECT geo IS NULL AS no_geo FROM event WHERE title = 'Geheimnisvolle Lesung'"
    ).fetchone()
    assert row["no_geo"]  # unknown stays unknown, not the source's point


def test_source_native_categories_never_reach_canon(conn):
    """Deterministic extractors pass raw source categories through
    ("Allgemein", "Schnellschach, Offen / Open" - found live 2026-07-09);
    canon publishes taxonomy values or unknown, never junk."""
    sid = _source(conn, "s1", 0.8)
    _claim(conn, sid, _concert("Schachturnier", category=("Schnellschach, Offen / Open", 0.9)),
           "schachturnier|2026-07-20|a")
    _claim(conn, sid, _concert("Konzert", category=("Music", 0.9)),
           "konzert|2026-07-20|b")
    rb.rebuild(conn, now=NOW)
    rows = {r["title"]: r["category"] for r in conn.execute(
        "SELECT title, category FROM event"
    ).fetchall()}
    assert rows["Schachturnier"] == []      # junk -> unknown, not published
    assert rows["Konzert"] == ["music"]     # case-normalized taxonomy value
