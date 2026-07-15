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


def test_more_specific_title_and_its_url_win_an_equal_weight_merge():
    common = {
        "source_id": uuid.uuid4(), "fingerprint": "same-event",
        "extracted_at": NOW, "trust": 0.8, "source_url": "https://source.at",
        "source_lat": None, "source_lon": None,
    }
    wrapper = rb.Claim(
        id=uuid.uuid4(), payload={
            "title": {"value": "Abendmusik in der", "confidence": 0.95},
            "url": {"value": "https://source.at/event/wrapper", "confidence": 0.95},
        }, **common,
    )
    specific = rb.Claim(
        id=uuid.uuid4(), payload={
            "title": {
                "value": "Abendmusik in der Ursulinenkirche: SAXESSOIRES",
                "confidence": 0.95,
            },
            "url": {"value": "https://source.at/event/saxessoires", "confidence": 0.95},
        }, **common,
    )

    values, _ = rb._merge_fields({"claims": [wrapper, specific]})

    assert values["title"] == \
        "Abendmusik in der Ursulinenkirche: SAXESSOIRES"
    assert values["url"] == "https://source.at/event/saxessoires"


def test_equal_timed_occurrences_prefer_the_specific_shorter_end():
    starts = datetime(2026, 7, 20, 18, 0, tzinfo=timezone.utc)
    wrapper_end = starts + timedelta(hours=3, minutes=59)
    performance_end = starts + timedelta(hours=1, minutes=30)

    folded = rb._fold_pairs([
        (starts, wrapper_end, True),
        (starts, performance_end, True),
        (starts, None, True),
    ])

    assert folded == [(starts, performance_end)]


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


def test_bare_validity_range_cannot_invent_daily_occurrences(conn):
    """A source's "bis <date>" validity range is not a daily cadence.

    This exact extractor error made Jazz im Musikpavillon appear on every day
    between its two real performances in production.
    """
    sid = _source(conn, "portal", 0.8)
    bad_daily = {
        "freq": "daily", "weekday": None, "week_of_month": None,
        "interval": 1, "time": None, "duration_minutes": None,
        "except_holidays": [], "valid_from": "2026-07-12",
        "valid_until": "2026-08-16", "as_stated": "bis 16.08.2026",
    }
    _claim(
        conn, sid,
        _concert(
            "Jazz im Musikpavillon 2026", starts="2026-07-12",
            venue="Musikpavillon", ends_at=("2026-08-16", 0.9),
            category=(["music"], 0.9), recurrence=(bad_daily, 0.9),
        ),
        "jazz musikpavillon|2026-07-12|deep",
    )
    for day in ("2026-07-17", "2026-07-19"):
        _claim(
            conn, sid,
            _concert(
                "Jazz im Musikpavillon 2026",
                starts=f"{day}T20:00:00+02:00", venue="Musikpavillon",
                ends_at=(f"{day}T23:59:00+02:00", 0.9),
                category=(["music"], 0.9),
            ),
            f"jazz musikpavillon|{day}|feed",
        )

    rb.rebuild(conn, now=NOW)

    events, occs = _canon(conn)
    assert len(events) == 1
    assert events[0]["kind"] == "series"
    assert {
        o["starts_at"].astimezone(rb.VIENNA).date().isoformat() for o in occs
    } == {"2026-07-12", "2026-07-17", "2026-07-19"}


def test_explicit_daily_wording_still_expands(conn):
    sid = _source(conn, "museum", 0.8)
    daily = {
        "freq": "daily", "weekday": None, "week_of_month": None,
        "interval": 1, "time": "10:00", "duration_minutes": 30,
        "except_holidays": [], "valid_from": "2026-07-12",
        "valid_until": "2026-07-14", "as_stated": "mehrmals am Tag",
    }
    _claim(
        conn, sid,
        _concert(
            "Deep Space Selection", starts="2026-07-12T10:00:00+02:00",
            venue="AEC", recurrence=(daily, 0.9),
        ),
        "deep space selection|2026-07-12|feed",
    )

    rb.rebuild(conn, now=NOW)

    events, occs = _canon(conn)
    assert len(events) == 1
    assert events[0]["kind"] == "series"
    assert {
        o["starts_at"].astimezone(rb.VIENNA).date().isoformat() for o in occs
    } == {"2026-07-12", "2026-07-13", "2026-07-14"}


def test_unrepresentable_daily_weekday_exceptions_do_not_expand(conn):
    """The v1 schema cannot encode daily-except-Tue/Sat; keep truth over recall."""
    sid = _source(conn, "stift", 0.8)
    unsupported = {
        "freq": "daily", "weekday": None, "week_of_month": None,
        "interval": 1, "time": "14:30", "duration_minutes": None,
        "except_holidays": [], "valid_from": "2026-05-06",
        "valid_until": "2026-10-26",
        "as_stated": (
            "Täglich von 6. Mai bis 26. Oktober "
            "(außer dienstags und samstags) um 14:30 Uhr"
        ),
    }
    _claim(
        conn, sid,
        _concert(
            "Orgelkurzkonzert an der Brucknerorgel",
            starts="2026-07-12T14:30:00+02:00",
            venue="Stiftsbasilika St. Florian",
            recurrence=(unsupported, 0.9),
        ),
        "orgelkurzkonzert brucknerorgel|2026-07-12|feed",
    )

    rb.rebuild(conn, now=NOW)

    events, occs = _canon(conn)
    assert len(events) == 1
    assert events[0]["kind"] == "one_off"
    assert len(occs) == 1
    assert occs[0]["starts_at"].astimezone(rb.VIENNA).date().isoformat() == \
        "2026-07-12"


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


def test_global_aggregator_junk_is_not_published(conn):
    """Eventbrite pads thin city listings with online/foreign events (live
    2026-07-10: Boston career fairs and a NASA launch served as Linz
    events). Global-platform-only + placeless + non-.at URL => unpublished;
    any .at URL or local corroboration keeps the event."""
    eb = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust) VALUES "
        "('Eventbrite Linz', 'https://www.eventbrite.at/d/linz/', 'website', 3, 0.6) "
        "RETURNING id"
    ).fetchone()["id"]
    local = _source(conn, "posthof", 0.9)

    def eb_claim(title, url, fp):
        fields = {"title": (title, 0.95),
                  "starts_at": ("2026-07-20T19:00:00+02:00", 0.95),
                  "url": (url, 0.95)}
        _claim(conn, eb, fields, fp)

    eb_claim("Boston Career Fair", "https://www.eventbrite.com/e/boston-1", "boston|x")
    eb_claim("Linzer Sommerkurs", "https://www.eventbrite.at/e/linz-1", "sommerkurs|x")
    # foreign URL but corroborated by a local source -> stays
    eb_claim("Kulturfest", "https://www.eventbrite.com/e/kultur-1", "kulturfest|x")
    _claim(conn, local, _concert("Kulturfest", venue=None), "kulturfest|x")
    rb.rebuild(conn, now=NOW)

    titles = {r["title"] for r in conn.execute("SELECT title FROM event").fetchall()}
    assert "Boston Career Fair" not in titles
    assert "Linzer Sommerkurs" in titles
    assert "Kulturfest" in titles


# ---------------------------------------------- audit 2026-07-12 regressions

_SOMMER_REC = {
    "freq": "weekly", "weekday": "TH", "interval": 1, "time": "18:00",
    "duration_minutes": None, "except_holidays": [], "valid_from": None,
    "valid_until": None, "as_stated": "jeden Donnerstag 18:00",
}


def test_wrong_weekday_rule_cannot_eat_the_series(conn):
    """A2a: a Thursdays-rule (shared description) was stamped onto the
    Fri/Sat claims of a daily series - 6 duplicate events, real dates lost."""
    sid = _source(conn, "s1", 0.8)
    for day, tag in (("2026-07-09", "a"), ("2026-07-10", "b"), ("2026-07-11", "c")):
        _claim(
            conn, sid,
            _concert("Sommerkonzerte im Musikpavillon",
                     starts=f"{day}T18:00:00+02:00", venue="Musikpavillon",
                     recurrence=(_SOMMER_REC, 0.9)),
            f"sommerkonzerte musikpavillon|{day}|{tag}",
        )
    rb.rebuild(conn, now=NOW)
    events, occs = _canon(conn)
    assert len(events) == 1  # one series, not one per weekday
    days = {o["starts_at"].date().isoformat() for o in occs}
    # observed Fri/Sat survive; the rule extends Thursdays
    assert {"2026-07-09", "2026-07-10", "2026-07-11"} <= days
    assert "2026-07-16" in days


def test_next_program_week_and_version_suffix_join_the_series(conn):
    """A2b: cinema listings spawned a new event per program week and per
    '(OmdtU)' title variant."""
    sid = _source(conn, "s1", 0.8)
    week1 = ("2026-07-06", "2026-07-07", "2026-07-08")
    week2 = ("2026-07-13", "2026-07-14")
    for d in week1 + week2:
        _claim(conn, sid,
               _concert("Backrooms", starts=f"{d}T20:45:00+02:00",
                        venue="Moviemento"),
               f"backrooms|{d}|9661:1901")
    _claim(conn, sid,
           _concert("Backrooms (OmdtU)", starts="2026-07-15T20:45:00+02:00",
                    venue="Moviemento"),
           "backrooms omdtu|2026-07-15|9661:1901")
    rb.rebuild(conn, now=NOW)
    events, occs = _canon(conn)
    assert len(events) == 1
    assert len(occs) == 6


def test_dateonly_claim_confirms_timed_occurrence(conn):
    """A9: a date-only claim put a midnight phantom next to the real 19:30
    occurrence on 404 event-days."""
    a = _source(conn, "s1", 0.8)
    b = _source(conn, "s2", 0.7)
    fp = "chet faker|2026-07-22|"
    _claim(conn, a, _concert("Chet Faker", starts="2026-07-22T19:30:00+02:00"), fp)
    _claim(conn, b, _concert("Chet Faker", starts="2026-07-22T00:00:00+02:00"), fp)
    rb.rebuild(conn, now=NOW)
    events, occs = _canon(conn)
    assert len(events) == 1
    assert len(occs) == 1
    assert occs[0]["starts_at"].astimezone(rb.VIENNA).hour == 19


def test_invalid_and_validity_spans_become_unknown_but_exhibitions_survive(conn):
    """Validity periods must not become continuous occurrences; genuine
    long exhibitions still use overlap semantics."""
    sid = _source(conn, "s1", 0.8)
    _claim(conn, sid,
           _concert("Friday Night Magic", starts="2026-07-10T18:00:00+02:00",
                    ends_at=("2028-07-13T22:00:00+02:00", 0.9)),
           "friday night magic|2026-07-10|v")
    _claim(conn, sid,
           _concert("HA", starts="2026-10-05T20:00:00+02:00",
                    ends_at=("2026-06-05T20:00:00+02:00", 0.9)),
           "ha|2026-10-05|v")
    _claim(conn, sid,
           _concert("Weekly Music Validity", starts="2026-07-10T18:00:00+02:00",
                    ends_at=("2026-08-31T18:00:00+02:00", 0.9),
                    category=(["music"], 0.9)),
           "weekly validity|2026-07-10|v")
    _claim(conn, sid,
           _concert("Summer Exhibition", starts="2026-07-10T10:00:00+02:00",
                    ends_at=("2026-10-10T18:00:00+02:00", 0.9),
                    category=(["art"], 0.9)),
           "summer exhibition|2026-07-10|v")
    rb.rebuild(conn, now=NOW)
    ends = {
        r["title"]: r["ends_at"]
        for r in conn.execute(
            "SELECT e.title, o.ends_at FROM occurrence o "
            "JOIN event e ON e.id = o.event_id"
        )
    }
    assert ends["Friday Night Magic"] is None
    assert ends["HA"] is None
    assert ends["Weekly Music Validity"] is None
    assert ends["Summer Exhibition"] is not None


def test_series_validity_end_cannot_span_later_performances(conn):
    """A run's final date is not one timed performance's DTEND.

    This production-shaped conflict made the 6 August Pretty Woman show fill
    nine calendar days even though later performances existed inside that
    purported occurrence.
    """
    sid = _source(conn, "musical-portal", 0.8)
    performances = (
        ("2026-08-06T19:30:00+02:00", "2026-08-16", "bad-validity-end"),
        ("2026-08-14T19:30:00+02:00", "2026-08-14T22:00:00+02:00", "fri"),
        ("2026-08-15T19:30:00+02:00", "2026-08-15T22:00:00+02:00", "sat"),
        ("2026-08-16T18:00:00+02:00", "2026-08-16T20:30:00+02:00", "sun"),
    )
    for starts, ends, tag in performances:
        _claim(
            conn, sid,
            _concert(
                "Pretty Woman – Das Musical", starts=starts,
                venue="Musiktheater", ends_at=(ends, 0.9),
                category=(["music"], 0.9),
            ),
            f"pretty woman|{starts[:10]}|{tag}",
        )

    rb.rebuild(conn, now=NOW)

    rows = conn.execute(
        "SELECT o.starts_at, o.ends_at FROM occurrence o "
        "JOIN event e ON e.id = o.event_id "
        "WHERE e.title = 'Pretty Woman – Das Musical' ORDER BY o.starts_at"
    ).fetchall()
    assert len(rows) == 4
    assert rows[0]["starts_at"].astimezone(rb.VIENNA).date().isoformat() == \
        "2026-08-06"
    assert rows[0]["ends_at"] is None
    assert all(
        row["ends_at"] - row["starts_at"] == timedelta(hours=2, minutes=30)
        for row in rows[1:]
    )


def test_standalone_multiday_event_keeps_its_real_end(conn):
    sid = _source(conn, "festival-site", 0.8)
    _claim(
        conn, sid,
        _concert(
            "Linzer Krone-Fest 2026", starts="2026-08-21T18:00:00+02:00",
            venue="Urfahrmarktgelände",
            ends_at=("2026-08-23T16:00:00+02:00", 0.9),
            category=(["music"], 0.9),
        ),
        "krone fest|2026-08-21|festival",
    )

    rb.rebuild(conn, now=NOW)

    row = conn.execute(
        "SELECT o.starts_at, o.ends_at FROM occurrence o "
        "JOIN event e ON e.id = o.event_id "
        "WHERE e.title = 'Linzer Krone-Fest 2026'"
    ).fetchone()
    assert row["ends_at"] - row["starts_at"] == timedelta(hours=46)


def test_no_event_row_ever_lacks_occurrences(conn):
    """A3: 115 rule-bearing events had DTSTART=rebuild-time and zero
    occurrences - invisible to every API read path. Observed claim dates
    are the floor now."""
    sid = _source(conn, "s1", 0.8)
    dead_rec = dict(_SOMMER_REC, weekday="MO", valid_until="2026-06-30",
                    as_stated="jeden Montag bis Ende Juni")
    _claim(conn, sid,
           _concert("Turnen", starts="2026-06-29T08:00:00+02:00",
                    venue="Turnhalle", recurrence=(dead_rec, 0.9)),
           "turnen|2026-06-29|v")
    # and a pure archive claim (audit A5: STWST shipped 2001-2019)
    _claim(conn, sid, _concert("Archivstück", starts="2004-09-09T22:00:00+02:00"),
           "archiv|2004-09-09|v")
    rb.rebuild(conn, now=NOW)
    orphans = conn.execute(
        "SELECT count(*) AS n FROM event e WHERE NOT EXISTS "
        "(SELECT 1 FROM occurrence o WHERE o.event_id = e.id)"
    ).fetchone()["n"]
    assert orphans == 0
    # the expired rule kept its observed date; the archive claim kept nothing
    titles = [r["title"] for r in conn.execute("SELECT title FROM event")]
    assert titles == ["Turnen"]
    assert conn.execute("SELECT count(*) AS n FROM occurrence").fetchone()["n"] == 1


def test_venueless_twin_joins_the_resolved_series(conn):
    """Post-repair follow-up 2026-07-13: 176 remaining dup pairs were all
    {no-venue, venue} twins of one series."""
    a = _source(conn, "s1", 0.8)
    b = _source(conn, "s2", 0.7)
    for d in ("2026-07-17", "2026-07-24", "2026-07-31"):
        _claim(conn, a, _concert("Basic Training", starts=f"{d}T19:15:00+02:00",
                                 venue="Sportunion Tanzsportklub"),
               f"basic training|{d}|va")
        # aggregator carries the same rows without a resolvable venue
        fields = _concert("Basic Training", starts=f"{d}T19:15:00+02:00")
        del fields["venue_name"]
        _claim(conn, b, fields, f"basic training|{d}|")
    rb.rebuild(conn, now=NOW)
    events, occs = _canon(conn)
    assert len(events) == 1
    observed = conn.execute(
        "SELECT count(*) AS n FROM occurrence WHERE NOT projected"
    ).fetchone()["n"]
    assert observed == 3  # the weekly cadence may project extras on top


def test_same_title_at_two_real_venues_stays_distinct(conn):
    a = _source(conn, "s1", 0.8)
    for d in ("2026-08-06", "2026-09-03", "2026-10-01"):
        _claim(conn, a, _concert("Clubabend", starts=f"{d}T19:30:00+02:00",
                                 venue="HTL Leonding"), f"clubabend|{d}|v1")
        _claim(conn, a, _concert("Clubabend", starts=f"{d}T19:30:00+02:00",
                                 venue="Naturfreundeheim Traun"), f"clubabend|{d}|v2")
    rb.rebuild(conn, now=NOW)
    events, _ = _canon(conn)
    assert len(events) == 2


def test_venueless_singleton_with_foreign_dates_stays_apart(conn):
    """Date corroboration required: a different 'Sommerfest' elsewhere on a
    different day must not be eaten by the venue-bearing series."""
    a = _source(conn, "s1", 0.8)
    for d in ("2026-07-17", "2026-07-24", "2026-07-31"):
        _claim(conn, a, _concert("Sommerfest", starts=f"{d}T18:00:00+02:00",
                                 venue="F10 Sportfabrik"), f"sommerfest|{d}|v")
    fields = _concert("Sommerfest", starts="2026-08-22T14:00:00+02:00")
    del fields["venue_name"]
    _claim(conn, a, fields, "sommerfest|2026-08-22|")
    rb.rebuild(conn, now=NOW)
    events, _ = _canon(conn)
    assert len(events) == 2


def test_venueless_twin_with_disjoint_dates_but_same_rule_merges(conn):
    """Prod 2026-07-13: MeinBezirk emits one row per date of a weekly
    course; venue present on some rows only. Disjoint observed days,
    identical rule -> one series."""
    rec = dict(_SOMMER_REC, weekday="FR", time="19:15",
               as_stated="jeden Freitag 19:15")
    sid = _source(conn, "meinbezirk", 0.4)
    _claim(conn, sid, _concert("Basic Training in Standard- und Lateintänzen",
                               starts="2026-07-10T19:15:00+02:00",
                               venue="Sportunion Tanzsportklub",
                               recurrence=(rec, 0.9)),
           "basic training standard lateintaenzen|2026-07-10|")
    f = _concert("Basic Training in Standard- und Lateintänzen",
                 starts="2026-07-17T19:15:00+02:00", recurrence=(rec, 0.9))
    del f["venue_name"]
    _claim(conn, sid, f, "basic training standard lateintaenzen|2026-07-17|")
    rb.rebuild(conn, now=NOW)
    events, _ = _canon(conn)
    assert len(events) == 1
