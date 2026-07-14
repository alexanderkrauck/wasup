"""API filter semantics - the null=unknown contract and keyset pagination."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from eventindex.api.app import app

NOW = datetime.now(timezone.utc)


def _add_event(conn, title, *, starts, lat=None, lon=None, category=None):
    event_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO event (id, kind, title, category, geo, confidence, status)
        VALUES (%(id)s, 'one_off', %(title)s, %(cats)s,
                CASE WHEN %(lat)s::float IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
                0.9, 'confirmed')
        """,
        {"id": event_id, "title": title, "cats": category or [], "lat": lat, "lon": lon},
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s)",
        (event_id, starts),
    )
    return event_id


@pytest.fixture
def client(conn):
    _add_event(conn, "Nearby Concert", starts=NOW + timedelta(days=1),
               lat=48.3069, lon=14.2858, category=["music"])
    _add_event(conn, "Far Away Fest", starts=NOW + timedelta(days=1),
               lat=48.15, lon=14.03, category=["music"])
    _add_event(conn, "Unknown Location Talk", starts=NOW + timedelta(days=2),
               category=["learning"])
    _add_event(conn, "No Category Thing", starts=NOW + timedelta(days=3))
    _add_event(conn, "Already Happened", starts=NOW - timedelta(days=2),
               lat=48.3069, lon=14.2858)
    conn.commit()
    return TestClient(app)


def _titles(resp):
    return [o["title"] for o in resp.json()["occurrences"]]


def test_default_excludes_past_and_gates_to_linz(client):
    titles = _titles(client.get("/v1/occurrences"))
    assert "Already Happened" not in titles
    # default 15km-around-Linz gate (2026-07-13): far events out, but
    # UNKNOWN locations stay in - null = unknown must not hide the index
    assert "Far Away Fest" not in titles
    assert "Unknown Location Talk" in titles
    assert len(titles) == 3
    # radius=any disables the gate
    all_titles = _titles(client.get("/v1/occurrences", params={"radius": "any"}))
    assert "Far Away Fest" in all_titles and len(all_titles) == 4


def test_near_filter_includes_only_known_close_geo(client):
    titles = _titles(client.get(
        "/v1/occurrences", params={"near": "48.3069,14.2858", "radius": "5km"}
    ))
    # null geo = unknown = never matches a geo filter
    assert titles == ["Nearby Concert"]


def test_category_filter_never_matches_unknown(client):
    titles = _titles(client.get("/v1/occurrences", params={"category": "music"}))
    assert "No Category Thing" not in titles
    assert "Nearby Concert" in titles


def test_keyset_pagination_walks_everything_once(client):
    seen = []
    cursor = None
    for _ in range(10):
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = client.get("/v1/occurrences", params=params).json()
        seen += [o["title"] for o in body["occurrences"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 3


def test_event_detail_404(client):
    assert client.get(f"/v1/events/{uuid.uuid4()}").status_code == 404


def test_reads_are_keyless_but_search_and_writes_are_gated(conn, client):
    conn.execute("INSERT INTO api_key (key, name) VALUES ('sekrit', 't')")
    conn.commit()
    # public reads: keyless, even with keys registered
    assert client.get("/v1/occurrences").status_code == 200
    assert client.post("/v1/query", json={}).status_code == 200
    assert client.get("/v1/feed.ics").status_code == 200
    assert client.get("/v1/changes").status_code == 200
    eid = conn.execute("SELECT id FROM event LIMIT 1").fetchone()["id"]
    assert client.get(f"/v1/events/{eid}").status_code == 200
    # budget-spending and writing endpoints stay keyed
    assert client.get("/v1/search", params={"q": "x"}).status_code == 401
    assert client.post(
        "/v1/reports", json={"occurrence_id": str(uuid.uuid4()), "reason": "wrong"}
    ).status_code == 401


def test_anonymous_reads_are_rate_limited(conn, client, monkeypatch):
    from eventindex.api import app as app_mod

    conn.execute("INSERT INTO api_key (key, name) VALUES ('sekrit', 't')")
    conn.commit()
    monkeypatch.setattr(app_mod, "PUBLIC_READ_RATE_PER_MIN", 3)
    app_mod._rate.clear()
    codes = [client.get("/v1/occurrences").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]
    # a key lifts the limit
    assert client.get(
        "/v1/occurrences", headers={"X-API-Key": "sekrit"}
    ).status_code == 200
    app_mod._rate.clear()


def test_feed_ics_serves_filtered_calendar(client):
    resp = client.get("/v1/feed.ics")
    assert resp.headers["content-type"].startswith("text/calendar")
    assert b"BEGIN:VEVENT" in resp.content
    assert b"Nearby Concert" in resp.content
    assert b"Already Happened" not in resp.content  # same from-now default
    only_music = client.get("/v1/feed.ics", params={"category": "learning"})
    assert b"Unknown Location Talk" in only_music.content
    assert b"Nearby Concert" not in only_music.content


def test_feed_can_exclude_known_adult_context_without_dropping_unknown(conn, client):
    adult_id = _add_event(
        conn, "Commercial Adult Venue Party", starts=NOW + timedelta(days=1),
        lat=48.3069, lon=14.2858, category=["nightlife"],
    )
    conn.execute(
        "UPDATE event SET inferred = %s WHERE id = %s",
        (Jsonb({"sex_service_context": {
            "value": True, "confidence": 0.8, "evidence": "venue",
        }}), adult_id),
    )
    conn.commit()
    default = client.get("/v1/feed.ics")
    assert b"Commercial Adult Venue Party" in default.content
    safe = client.get(
        "/v1/feed.ics", params={"exclude_sex_service_context": "true"}
    )
    assert b"Commercial Adult Venue Party" not in safe.content
    assert b"Unknown Location Talk" in safe.content


def test_report_enqueues_qa_check(conn, client):
    oid = conn.execute("SELECT id FROM occurrence LIMIT 1").fetchone()["id"]
    resp = client.post(
        "/v1/reports",
        json={"occurrence_id": str(oid), "reason": "cancelled", "note": "war abgesagt"},
    )
    assert resp.status_code == 202
    job = conn.execute("SELECT payload FROM jobs WHERE kind = 'qa_check'").fetchone()
    assert job["payload"]["occurrence_id"] == str(oid)
    assert conn.execute("SELECT count(*) AS n FROM report").fetchone()["n"] == 1
    missing = client.post(
        "/v1/reports", json={"occurrence_id": str(uuid.uuid4()), "reason": "wrong"}
    )
    assert missing.status_code == 404


def test_changes_keyset_cursor_walks_everything_once(client):
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 2}
        if cursor:
            params["since"] = cursor
        body = client.get("/v1/changes", params=params).json()
        seen += [e["id"] for e in body["events"]]
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 5


def test_query_endpoint_needs_no_llm_and_accepts_partial_filters(client):
    resp = client.post("/v1/query", json={"categories": ["music"]})
    assert resp.status_code == 200
    data = resp.json()
    titles = [o["title"] for o in data["occurrences"]]
    assert "Nearby Concert" in titles
    assert "No Category Thing" not in titles  # null category = unknown (hard)
    assert all("match_score" in o for o in data["occurrences"])


def test_query_endpoint_soft_preferences_keep_unknowns(client):
    resp = client.post("/v1/query", json={
        "kid_friendly": True, "importance": {"kid_friendly": 0.8},
    })
    assert resp.status_code == 200
    # no event has kid_friendly data -> all stay visible, scored at the prior
    # (3: the default Linz gate excludes Far Away Fest)
    assert len(resp.json()["occurrences"]) == 3


def test_query_endpoint_rejects_garbage(client):
    assert client.post("/v1/query", json={"nonsense_field": 1}).status_code == 422
    assert client.post("/v1/query", json={"from_dt": "tomorrow"}).status_code == 422
    assert client.post(
        "/v1/query", json={"importance": {"not_an_attr": 1.0}}
    ).status_code == 422
    assert client.post(
        "/v1/query", json={"importance": {"kid_friendly": 7}}
    ).status_code == 422
    assert client.post(
        "/v1/query", json={"required_attributes": ["favourite_color"]}
    ).status_code == 422


def test_query_body_is_documented_in_openapi(client):
    schema = client.get("/openapi.json").json()
    body = schema["components"]["schemas"]["QueryBody"]["properties"]
    assert "importance" in body and "gender_split_min" in body
    assert "certainty" in body["importance"]["description"]


def test_discovery_surfaces_are_open_even_when_keys_exist(conn, client):
    conn.execute("INSERT INTO api_key (key, name) VALUES ('sekrit', 't')")
    conn.commit()
    llms = client.get("/llms.txt")
    assert llms.status_code == 200  # open by design, like /docs
    assert "music" in llms.text  # taxonomy injected
    assert "/v1/query" in llms.text
    catalog = client.get("/.well-known/api-catalog")
    assert catalog.status_code == 200
    assert "openapi.json" in catalog.text
    assert client.get("/v1/search", params={"q": "x"}).status_code == 401  # budget stays keyed


def test_staleness_decay_is_computed_at_query_time(conn):
    event_id = _add_event(conn, "Zombie Stammtisch", starts=NOW + timedelta(days=1))
    # confirmed a month ago, weekly cadence -> 0.9^4 ≈ 0.59 effective
    conn.execute(
        "UPDATE event SET expected_cadence = interval '7 days' WHERE id = %s",
        (event_id,),
    )
    conn.execute(
        "UPDATE occurrence SET last_confirmed_at = now() - interval '30 days' "
        "WHERE event_id = %s",
        (event_id,),
    )
    conn.commit()
    client = TestClient(app)

    fresh = client.get("/v1/occurrences", params={"min_confidence": 0.8})
    assert "Zombie Stammtisch" not in _titles(fresh)  # stored 0.9 has decayed
    lenient = client.get("/v1/occurrences", params={"min_confidence": 0.5})
    assert "Zombie Stammtisch" in _titles(lenient)
    served = next(
        o for o in client.get("/v1/occurrences").json()["occurrences"]
        if o["title"] == "Zombie Stammtisch"
    )
    assert 0.55 < served["confidence"] < 0.65  # 0.9 × 0.9^4


def test_event_detail_serializes_enriched_events(conn, client):
    """int4range/interval columns 500ed the detail endpoint for every
    enriched event (found by the first external consumer, 2026-07-09)."""
    event_id = conn.execute("SELECT id FROM event LIMIT 1").fetchone()["id"]
    conn.execute(
        "UPDATE event SET expected_age_range = int4range(20, 30, '[]'), "
        "expected_cadence = interval '7 days' WHERE id = %s", (event_id,),
    )
    conn.commit()
    resp = client.get(f"/v1/events/{event_id}")
    assert resp.status_code == 200
    assert resp.json()["event"]["expected_age_range"] == "[20, 31)"


def test_query_rows_carry_venue(conn, client):
    vid = conn.execute(
        "INSERT INTO venue (name, address) VALUES ('Posthof', 'Posthofstr. 43') "
        "RETURNING id"
    ).fetchone()["id"]
    conn.execute("UPDATE event SET venue_id = %s WHERE title = 'Nearby Concert'", (vid,))
    conn.commit()
    rows = client.post("/v1/query", json={}).json()["occurrences"]
    concert = next(r for r in rows if r["title"] == "Nearby Concert")
    assert concert["venue_name"] == "Posthof"
    assert concert["venue_address"] == "Posthofstr. 43"


def test_query_get_variant_for_browse_only_agents(conn, client):
    """ChatGPT's browsing tool can only GET (found live, 2026-07-09)."""
    resp = client.get(
        "/v1/query",
        params={"categories": "music", "kid_friendly": "true",
                "importance": "kid_friendly:0.8", "limit": 5},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert [o["title"] for o in data["occurrences"]] != []
    assert data["parsed_filters"]["categories"] == ["music"]
    assert data["importance"] == {"kid_friendly": 0.8}
    assert client.get("/v1/query", params={"bogus": "1"}).status_code == 422
    assert client.get(
        "/v1/query", params={"importance": "kid_friendly"}
    ).status_code == 422


# ------------------------------------------ audit 2026-07-12 fixes (Block 5)

def test_ongoing_occurrence_is_visible_with_flag(client, conn):
    """A21: 95 running exhibitions were invisible under starts_at-only
    windows; overlap semantics is the default since 2026-07-13."""
    eid = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, category, confidence, status) "
        "VALUES (%s, 'one_off', 'Laufende Ausstellung', '{art}', 0.9, 'confirmed')",
        (eid,),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at, ends_at) VALUES (%s, %s, %s)",
        (eid, NOW - timedelta(days=5), NOW + timedelta(days=5)),
    )
    conn.commit()
    body = client.get("/v1/occurrences").json()
    row = next(o for o in body["occurrences"] if o["title"] == "Laufende Ausstellung")
    assert row["ongoing"] is True
    body = client.post("/v1/query", json={}).json()
    row = next(o for o in body["occurrences"] if o["title"] == "Laufende Ausstellung")
    assert row["ongoing"] is True


def test_unknown_category_is_422_not_empty(client):
    """B3: a typo'd category silently returned nothing; in
    exclude_categories it silently weakened a guarantee."""
    assert client.post("/v1/query", json={"categories": ["konzert"]}).status_code == 422
    assert client.get("/v1/query", params={"categories": "konzert"}).status_code == 422
    assert client.post(
        "/v1/query", json={"exclude_categories": ["nightlfe"]}
    ).status_code == 422


def test_impossible_ranges_are_422(client):
    assert client.post("/v1/query", json={
        "from_dt": "2026-08-01", "to_dt": "2026-07-01",
    }).status_code == 422
    assert client.post("/v1/query", json={
        "age_min": 60, "age_max": 20,
    }).status_code == 422


def test_distinct_event_and_sort_starts_at(client, conn):
    eid = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, category, confidence, status) "
        "VALUES (%s, 'series', 'Tagesführung', '{culture}', 0.9, 'confirmed')",
        (eid,),
    )
    for d in (1, 2, 3):
        conn.execute(
            "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s)",
            (eid, NOW + timedelta(days=d)),
        )
    conn.commit()
    rows = client.post("/v1/query?distinct=event", json={}).json()["occurrences"]
    assert sum(1 for r in rows if r["title"] == "Tagesführung") == 1  # B1
    rows = client.post("/v1/query?sort=starts_at", json={}).json()["occurrences"]
    starts = [r["starts_at"] for r in rows]
    assert starts == sorted(starts)  # B2


def test_to_dt_bare_date_covers_the_whole_day(client, conn):
    eid = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, category, confidence, status) "
        "VALUES (%s, 'one_off', 'Abendkonzert 18ter', '{music}', 0.9, 'confirmed')",
        (eid,),
    )
    evening = (NOW + timedelta(days=3)).replace(hour=19)
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s)", (eid, evening),
    )
    conn.commit()
    day = evening.date().isoformat()
    rows = client.post("/v1/query", json={"to_dt": day}).json()["occurrences"]
    assert any(r["title"] == "Abendkonzert 18ter" for r in rows)  # B6
