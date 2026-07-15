"""MCP surface at /mcp: tool contracts both directories review (titles +
readOnly annotations), the ChatGPT-required search/fetch pair, and the
keyless-but-rate-limited gate. Stateless JSON mode means plain JSON-RPC
POSTs work - no session handshake needed."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from eventindex.api.app import app
from test_api import _add_event

NOW = datetime.now(timezone.utc)

_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


@pytest.fixture(scope="module")
def _lifespan_client():
    """One client for the whole module: the SDK's session manager allows
    exactly one .run() per instance (in production the lifespan also runs
    once per process). Only this module may enter the app's lifespan."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def client(conn, _lifespan_client):
    _add_event(conn, "Salsa Social", starts=NOW + timedelta(days=1),
               lat=48.30, lon=14.29, category=["nightlife"])
    _add_event(conn, "Chamber Concert", starts=NOW + timedelta(days=2),
               category=["music"])
    conn.commit()
    return _lifespan_client


def _rpc(client, method, params=None, id=1):
    resp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": id, "method": method,
              "params": params or {}},
        headers=_HEADERS,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _call(client, tool, arguments):
    body = _rpc(client, "tools/call", {"name": tool, "arguments": arguments})
    assert not body["result"].get("isError"), body["result"]
    return body["result"]["structuredContent"]


def _call_result(client, tool, arguments):
    body = _rpc(client, "tools/call", {"name": tool, "arguments": arguments})
    assert not body["result"].get("isError"), body["result"]
    return body["result"]


def _mark_sex_service(conn, event_id, value=True):
    conn.execute(
        "UPDATE event SET inferred = %s WHERE id = %s",
        (Jsonb({
            "sex_service_context": {
                "value": value, "confidence": 0.8,
                "evidence": "private raw evidence must never be served",
            },
            "vibe_tags": ["nightlife"],
        }), event_id),
    )


def test_tools_carry_directory_required_annotations(client):
    tools = _rpc(client, "tools/list")["result"]["tools"]
    names = {t["name"] for t in tools}
    assert names == {"search_events", "get_event", "get_calendar_link",
                     "search", "fetch"}
    for t in tools:
        # missing titles/annotations are a standard directory rejection
        assert t["title"], t["name"]
        assert t["annotations"]["readOnlyHint"] is True, t["name"]
        assert t["annotations"]["destructiveHint"] is False, t["name"]
        assert t["annotations"]["openWorldHint"] is False, t["name"]
        assert t["outputSchema"]["type"] == "object", t["name"]
        assert t["outputSchema"].get("additionalProperties") is False, t["name"]
        assert t["outputSchema"].get("properties"), t["name"]
        assert t["description"].startswith("Use this when"), t["name"]


def test_search_events_runs_the_query_core(client):
    result = _call(client, "search_events",
                   {"filters": {"categories": ["nightlife"]}, "limit": 5})
    titles = [o["title"] for o in result["occurrences"]]
    assert titles == ["Salsa Social"]
    assert all("match_score" in o for o in result["occurrences"])


def test_search_events_rejects_unknown_filters(client):
    body = _rpc(client, "tools/call", {
        "name": "search_events", "arguments": {"filters": {"bogus": 1}},
    })
    assert "error" in body or body["result"].get("isError")


def test_chatgpt_connector_search_fetch_contract(client):
    search_result = _call_result(client, "search", {"query": "salsa"})
    assert len(search_result["content"]) == 1
    assert search_result["content"][0]["type"] == "text"
    assert json.loads(search_result["content"][0]["text"]) == \
        search_result["structuredContent"]
    results = search_result["structuredContent"]["results"]
    assert results and set(results[0]) == {"id", "title", "url"}
    assert "Salsa Social" in results[0]["title"]
    prompt_results = _call(client, "search", {
        "query": "Search the Linz event index for salsa",
    })["results"]
    assert prompt_results and "Salsa Social" in prompt_results[0]["title"]
    fetch_result = _call_result(client, "fetch", {"id": results[0]["id"]})
    assert len(fetch_result["content"]) == 1
    assert json.loads(fetch_result["content"][0]["text"]) == \
        fetch_result["structuredContent"]
    doc = fetch_result["structuredContent"]
    assert {"id", "title", "text", "url", "metadata"} <= set(doc)
    assert "Salsa Social" in doc["text"]
    assert doc["url"].startswith("https://wasup.at/v1/events/")


def test_get_calendar_link_builds_ics_url(client):
    out = _call(client, "get_calendar_link",
                {"category": "dance", "from_dt": "2026-07-09T00:00:00"})
    assert "/v1/feed.ics?" in out["ics_url"]
    assert "category=dance" in out["ics_url"]
    assert "exclude_sex_service_context=true" in out["ics_url"]
    assert "include_time_unknown=false" in out["ics_url"]

    with_unknown_times = _call(client, "get_calendar_link", {
        "category": "dance", "include_time_unknown": True,
    })
    assert "include_time_unknown=true" in with_unknown_times["ics_url"]


def test_get_calendar_link_rejects_an_unscoped_subscription(client):
    body = _rpc(client, "tools/call", {
        "name": "get_calendar_link", "arguments": {},
    })
    assert body["result"].get("isError") is True
    assert "category" in body["result"]["content"][0]["text"].lower()


def test_get_event_detail(conn, client):
    eid = conn.execute("SELECT id FROM event LIMIT 1").fetchone()["id"]
    out = _call(client, "get_event", {"event_id": str(eid)})
    assert out["event"]["id"] == str(eid) or out["event"]["id"] == eid
    assert "sources" in out and "occurrences" in out
    assert "claims" not in out


def test_adult_context_is_default_denied_but_explicitly_available(conn, client):
    adult_id = _add_event(
        conn, "Commercial Adult Venue Party", starts=NOW + timedelta(days=1),
        lat=48.30, lon=14.29, category=["nightlife"],
    )
    _mark_sex_service(conn, adult_id)
    venue_id = conn.execute(
        "INSERT INTO venue (name, sex_service) VALUES ('Curated Adult Venue', true) "
        "RETURNING id"
    ).fetchone()["id"]
    venue_only_id = _add_event(
        conn, "Innocuous Title At Curated Venue",
        starts=NOW + timedelta(days=2), lat=48.30, lon=14.29,
        category=["sport"],
    )
    conn.execute(
        "UPDATE event SET venue_id = %s, inferred = NULL WHERE id = %s",
        (venue_id, venue_only_id),
    )
    conn.commit()

    default = _call(client, "search_events", {"limit": 100})
    default_ids = {uuid.UUID(o["event_id"]) for o in default["occurrences"]}
    assert adult_id not in default_ids
    assert venue_only_id not in default_ids
    explicit_false = _call(client, "search_events", {
        "filters": {"sex_service_context": False}, "limit": 100,
    })
    assert adult_id not in {
        uuid.UUID(o["event_id"]) for o in explicit_false["occurrences"]
    }
    explicit_true = _call(client, "search_events", {
        "filters": {"sex_service_context": True}, "limit": 100,
    })
    explicit_true_ids = {
        uuid.UUID(o["event_id"]) for o in explicit_true["occurrences"]
    }
    assert adult_id in explicit_true_ids
    assert venue_only_id in explicit_true_ids

    denied = _rpc(client, "tools/call", {
        "name": "get_event", "arguments": {"event_id": str(adult_id)},
    })
    assert denied["result"]["isError"] is True
    allowed = _call(client, "get_event", {
        "event_id": str(adult_id), "include_sex_service_context": True,
    })
    assert allowed["event"]["id"] == str(adult_id)
    assert allowed["event"]["estimates"]["sex_service_context"]["value"] is True

    assert _call(client, "search", {"query": "commercial adult venue"})["results"] == []
    denied_fetch = _rpc(client, "tools/call", {
        "name": "fetch", "arguments": {"id": str(adult_id)},
    })
    assert denied_fetch["result"]["isError"] is True
    venue_denied = _rpc(client, "tools/call", {
        "name": "get_event", "arguments": {"event_id": str(venue_only_id)},
    })
    assert venue_denied["result"]["isError"] is True


def test_standard_search_is_hard_relevant_future_and_distinct(conn, client):
    future_id = _add_event(
        conn, "HWYD Social Run", starts=NOW + timedelta(days=3),
        lat=48.30, lon=14.29, category=["sport"],
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s)",
        (future_id, NOW + timedelta(days=4)),
    )
    ongoing_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, category, confidence, status) "
        "VALUES (%s, 'one_off', 'Ongoing Run Exhibition', '{sport}', 0.9, 'confirmed')",
        (ongoing_id,),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at, ends_at) VALUES (%s, %s, %s)",
        (ongoing_id, NOW - timedelta(days=2), NOW + timedelta(days=2)),
    )
    polluted_id = _add_event(
        conn, "Football Practice", starts=NOW + timedelta(days=2),
        lat=48.30, lon=14.29, category=["sport"],
    )
    conn.execute(
        "UPDATE event SET inferred = %s WHERE id = %s",
        (Jsonb({"vibe_tags": ["run"]}), polluted_id),
    )
    exact_phrase_id = _add_event(
        conn, "Football Lounge Nights Special",
        starts=NOW + timedelta(days=2), lat=48.30, lon=14.29,
        category=["sport"],
    )
    filler_id = _add_event(
        conn, "Keramik Special", starts=NOW + timedelta(days=2),
        lat=48.30, lon=14.29, category=["culture"],
    )
    conn.commit()

    results = _call(client, "search", {
        "query": "running events in Linz",
    })["results"]
    ids = [uuid.UUID(result["id"]) for result in results]
    assert ids.count(future_id) == 1
    assert ongoing_id not in ids
    assert polluted_id not in ids
    assert all(
        any(term in result["title"].lower() for term in ("run", "lauf", "jogging"))
        for result in results
    )
    phrase_results = _call(client, "search", {
        "query": "football lounge nights special",
    })["results"]
    phrase_ids = {uuid.UUID(result["id"]) for result in phrase_results}
    assert exact_phrase_id in phrase_ids
    assert filler_id not in phrase_ids


def test_search_events_places_in_window_starts_before_ongoing(conn, client):
    ongoing_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, category, confidence, status) "
        "VALUES (%s, 'one_off', 'Long Exhibition', '{art}', 0.9, 'confirmed')",
        (ongoing_id,),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at, ends_at) VALUES (%s, %s, %s)",
        (ongoing_id, NOW - timedelta(days=5), NOW + timedelta(days=5)),
    )
    conn.commit()
    out = _call(client, "search_events", {
        "filters": {
            "from_dt": NOW.isoformat(),
            "to_dt": (NOW + timedelta(days=3)).isoformat(),
        },
        "limit": 100,
        "sort": "starts_at",
    })
    rows = out["occurrences"]
    first_ongoing = next(i for i, row in enumerate(rows) if row["ongoing"])
    assert all(not row["ongoing"] for row in rows[:first_ongoing])
    assert any(uuid.UUID(row["event_id"]) == ongoing_id for row in rows[first_ongoing:])


def test_event_detail_never_returns_raw_claim_payload(conn, client):
    event_id = conn.execute(
        "SELECT id FROM event WHERE title = 'Salsa Social'"
    ).fetchone()["id"]
    source_id = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust) "
        "VALUES ('Public Source', 'https://source.example/events', 'website', 2, 0.8) "
        "RETURNING id"
    ).fetchone()["id"]
    fingerprint = "private-detail-regression"
    conn.execute(
        "INSERT INTO event_claim (source_id, fingerprint, raw_excerpt, payload) "
        "VALUES (%s, %s, %s, %s)",
        (source_id, fingerprint, "SECRET PRIVATE ADDRESS", Jsonb({
            "address": {"value": "SECRET PRIVATE ADDRESS", "confidence": 1},
            "url": {
                "value": "https://source.example/events/correct-event",
                "confidence": 1,
            },
        })),
    )
    conn.execute(
        "INSERT INTO identity (fingerprint, event_id) VALUES (%s, %s)",
        (fingerprint, event_id),
    )
    conn.execute(
        "UPDATE event SET inferred = %s WHERE id = %s",
        (Jsonb({"vibe_tags": [f"tag-{i}" for i in range(20)]}), event_id),
    )
    conn.commit()

    mcp_detail = _call(client, "get_event", {"event_id": str(event_id)})
    assert "SECRET PRIVATE ADDRESS" not in json.dumps(mcp_detail)
    assert mcp_detail["sources"][0]["name"] == "Public Source"
    assert mcp_detail["sources"][0]["url"] == \
        "https://source.example/events/correct-event"
    assert len(mcp_detail["event"]["estimates"]["vibe_tags"]) == 6
    public_detail = client.get(f"/v1/events/{event_id}").json()
    assert "claims" not in public_detail
    assert "SECRET PRIVATE ADDRESS" not in json.dumps(public_detail)


def test_submission_artifact_has_exact_stable_case_contract():
    submission = json.loads(
        (Path(__file__).parents[1] / "chatgpt-app-submission.json").read_text()
    )
    assert len(submission["test_cases"]) == 5
    assert len(submission["negative_test_cases"]) == 3
    tools = set()
    for case in submission["test_cases"]:
        tools.update(t.strip() for t in case["tools_triggered"].split(","))
        rendered = json.dumps(case)
        assert "wasup.goedly.com" not in rendered
    assert tools == {"search_events", "get_event", "get_calendar_link", "search", "fetch"}


def test_mcp_is_keyless_but_rate_limited(conn, client, monkeypatch):
    from eventindex.api import app as app_mod

    conn.execute("INSERT INTO api_key (key, name) VALUES ('sekrit', 't')")
    conn.commit()
    monkeypatch.setattr(app_mod, "PUBLIC_READ_RATE_PER_MIN", 3)
    app_mod._rate.clear()
    codes = [
        client.post("/mcp", json={"jsonrpc": "2.0", "id": i,
                                  "method": "tools/list", "params": {}},
                    headers=_HEADERS).status_code
        for i in range(5)
    ]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:]
    # a key lifts the limit
    assert client.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 9, "method": "tools/list",
                      "params": {}},
        headers={**_HEADERS, "X-API-Key": "sekrit"},
    ).status_code == 200
    app_mod._rate.clear()
