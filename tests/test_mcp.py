"""MCP surface at /mcp: tool contracts both directories review (titles +
readOnly annotations), the ChatGPT-required search/fetch pair, and the
keyless-but-rate-limited gate. Stateless JSON mode means plain JSON-RPC
POSTs work - no session handshake needed."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

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
               lat=48.30, lon=14.29, category=["dance"])
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


def test_search_events_runs_the_query_core(client):
    result = _call(client, "search_events",
                   {"filters": {"categories": ["dance"]}, "limit": 5})
    titles = [o["title"] for o in result["occurrences"]]
    assert titles == ["Salsa Social"]
    assert all("match_score" in o for o in result["occurrences"])


def test_search_events_rejects_unknown_filters(client):
    body = _rpc(client, "tools/call", {
        "name": "search_events", "arguments": {"filters": {"bogus": 1}},
    })
    assert "error" in body or body["result"].get("isError")


def test_chatgpt_connector_search_fetch_contract(client):
    results = _call(client, "search", {"query": "salsa"})["results"]
    assert results and set(results[0]) == {"id", "title", "url"}
    assert "Salsa Social" in results[0]["title"]
    doc = _call(client, "fetch", {"id": results[0]["id"]})
    assert {"id", "title", "text", "url", "metadata"} <= set(doc)
    assert "Salsa Social" in doc["text"]


def test_get_calendar_link_builds_ics_url(client):
    out = _call(client, "get_calendar_link",
                {"category": "dance", "from_dt": "2026-07-09T00:00:00"})
    assert "/v1/feed.ics?" in out["ics_url"]
    assert "category=dance" in out["ics_url"]


def test_get_event_detail(conn, client):
    eid = conn.execute("SELECT id FROM event LIMIT 1").fetchone()["id"]
    out = _call(client, "get_event", {"event_id": str(eid)})
    assert out["event"]["id"] == str(eid) or out["event"]["id"] == eid
    assert "claims" in out and "occurrences" in out


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
