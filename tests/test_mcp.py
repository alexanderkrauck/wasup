"""MCP surface at /mcp: tool contracts both directories review (titles +
readOnly annotations), the ChatGPT-required search/fetch pair, and the
keyless-but-rate-limited gate. Stateless JSON mode means plain JSON-RPC
POSTs work - no session handshake needed."""

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

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
        "UPDATE event SET inferred = inferred || %s WHERE id = %s",
        (Jsonb({
            "sex_service_context": {
                "value": value, "confidence": 0.8,
                "evidence": "private raw evidence must never be served",
            },
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
        assert t["inputSchema"].get("additionalProperties") is False, t["name"]
        assert t["outputSchema"]["type"] == "object", t["name"]
        assert t["outputSchema"].get("additionalProperties") is False, t["name"]
        assert t["outputSchema"].get("properties"), t["name"]
        assert t["description"].startswith("Use this when"), t["name"]


def test_tool_metadata_teaches_one_call_composition_and_hard_soft_intent(client):
    tools = {
        tool["name"]: tool
        for tool in _rpc(client, "tools/list")["result"]["tools"]
    }
    search_description = tools["search_events"]["description"]
    assert '"name":"ball"' in search_description
    assert '"tags":["dance","elegant"]' in search_description
    assert '"source":"WKO"' in search_description
    assert "one call" in search_description.lower()
    assert "preferred_max_price" in search_description
    assert "max_price" in search_description
    current_schema = json.dumps(tools["search_events"]["inputSchema"])
    assert "include_terms" not in current_schema
    assert "vibe_terms" not in current_schema
    calendar_description = tools["get_calendar_link"]["description"]
    assert "same `filters` object" in calendar_description
    assert "required_attributes" in calendar_description
    assert "weakest accepted result" in calendar_description


def test_search_events_runs_the_query_core(client):
    result = _call(client, "search_events",
                   {"filters": {"categories": ["nightlife"]}, "limit": 5})
    titles = [o["title"] for o in result["occurrences"]]
    assert titles == ["Salsa Social"]
    assert all("match_score" in o for o in result["occurrences"])


def test_search_events_returns_multi_tag_evidence_without_contract_error(
    conn, client, monkeypatch,
):
    from eventindex import tags as tag_store

    event_id = _add_event(
        conn, "Singing Circle", starts=NOW + timedelta(days=3),
        lat=48.30, lon=14.29, category=["nightlife"],
    )
    conn.commit()

    def semantic_matches(tx, event_ids, desired):
        assert desired == ["singing", "movement"]
        concepts = []
        for query, event_tag, score, joint in (
            ("singing", "singing circle", 0.882657, False),
            ("movement", "movement", 0.154198, False),
            ("singing + movement", "sound meditation", 0.745269, True),
        ):
            support = {
                "score": score,
                "event_tag": event_tag,
                "tag_confidence": 0.8,
                "relatedness": score / 0.8,
                "origin": "event_tag",
            }
            concepts.append({
                "query": query,
                "score": score,
                "event_tag": event_tag,
                "tag_confidence": 0.8,
                "relatedness": score / 0.8,
                "origin": "event_tag",
                "supports": [support],
                "joint": joint,
                "role": (
                    "combined_phrase_context" if joint else "requested_concept"
                ),
            })
        return {
            candidate: {
                # Regression for the production Singing Circle result: the
                # strong combined-phrase diagnostic must never be presented
                # as the final multi-concept coverage score.
                "score": 0.262533 if candidate == event_id else 0.0,
                "concepts": concepts,
                "weakest_concept_score": (
                    0.154198 if candidate == event_id else 0.0
                ),
                "weakest_concept_query": "movement",
                "combined_context_score": (
                    0.745269 if candidate == event_id else 0.0
                ),
            }
            for candidate in event_ids
        }

    monkeypatch.setattr(tag_store, "semantic_matches", semantic_matches)
    result = _call(client, "search_events", {
        "filters": {
            "name": "Singing Circle",
            "tags": ["singing", "movement"],
            "importance": {"tags": 1.0},
        },
        "limit": 8,
    })

    assert [row["title"] for row in result["occurrences"]] == ["Singing Circle"]
    matches = result["occurrences"][0]["tag_matches"]
    assert len(matches) == 3
    assert matches[-1]["joint"] is True
    assert matches[-1]["role"] == "combined_phrase_context"
    assert matches[0]["origin"] == "event_tag"
    assert matches[0]["supports"][0]["origin"] == "event_tag"
    row = result["occurrences"][0]
    assert row["tag_match"] == 0.2625
    assert row["tag_weakest_concept_match"] == 0.1542
    assert row["tag_weakest_concept"] == "movement"
    assert row["tag_context_match"] == 0.7453
    assert row["tag_context_match"] != row["tag_match"]


def test_search_events_rejects_unknown_filters(client):
    body = _rpc(client, "tools/call", {
        "name": "search_events", "arguments": {"filters": {"bogus": 1}},
    })
    assert "error" in body or body["result"].get("isError")


def test_search_events_rejects_flattened_filters_instead_of_searching_broadly(
    client,
):
    body = _rpc(client, "tools/call", {
        "name": "search_events",
        "arguments": {
            "name": "ball",
            "tags": ["dance", "elegant"],
            "limit": 100,
        },
    })
    assert body["result"].get("isError") is True
    message = body["result"]["content"][0]["text"]
    assert "Unknown top-level arguments" in message
    assert "name" in message and "tags" in message


def test_chatgpt_connector_search_fetch_contract(client):
    search_result = _call_result(client, "search", {"query": "salsa"})
    assert len(search_result["content"]) == 1
    assert search_result["content"][0]["type"] == "text"
    assert json.loads(search_result["content"][0]["text"]) == \
        search_result["structuredContent"]
    results = search_result["structuredContent"]["results"]
    assert results and set(results[0]) == {"id", "title", "url"}
    assert "Salsa Social" in results[0]["title"]
    # prompt-wrapper queries are the calling model's job to translate; the
    # empty result carries the steering hint instead of degrading into filler
    prompt_out = _call(client, "search", {
        "query": "Search the Linz event index for anything nice",
    })
    assert prompt_out["results"] == []
    assert "search_events" in prompt_out["hint"]
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
                {"filters": {
                    "categories": ["nightlife"],
                    "from_dt": "2026-07-09T00:00:00",
                }})
    assert "/v1/feed.ics?" in out["ics_url"]
    assert "category=nightlife" in out["ics_url"]
    assert "exclude_sex_service_context=true" in out["ics_url"]
    assert "include_time_unknown=false" in out["ics_url"]
    assert out["coverage_complete"] is None
    assert out["coverage"] == []

    with_unknown_times = _call(client, "get_calendar_link", {
        "filters": {"categories": ["nightlife"]},
        "include_time_unknown": True,
    })
    assert "min_confidence=0.4" in out["ics_url"]
    assert "include_time_unknown=true" in with_unknown_times["ics_url"]

    semantic = _call(client, "get_calendar_link", {
        "filters": {
            "tags": ["salsa dancing"], "min_tag_match": 0.6,
            "min_tag_concept_match": 0.4,
        },
    })
    assert "tags=salsa+dancing" in semantic["ics_url"]
    assert "min_tag_match=0.6" in semantic["ics_url"]
    assert "min_tag_concept_match=0.4" in semantic["ics_url"]

    tentative = _call(client, "get_calendar_link", {
        "filters": {
            "tags": ["salsa dancing"], "min_tag_match": 0.6,
            "min_confidence": 0,
        },
    })
    assert "min_confidence=0.0" in tentative["ics_url"]

    organizer = _call(client, "get_calendar_link", {
        "filters": {
            "organizer": "WKO", "tags": ["startup"], "min_tag_match": 0.2,
        },
    })
    assert "organizer=WKO" in organizer["ics_url"]
    assert "tags=startup" in organizer["ics_url"]

    source = _call(client, "get_calendar_link", {
        "filters": {
            "source": "WKO", "tags": ["startup"], "min_tag_match": 0.2,
            "weekdays": ["thursday", "friday"],
        },
    })
    assert "source=WKO" in source["ics_url"]
    assert "min_tag_match=0.2" in source["ics_url"]
    assert "weekdays=thursday%2Cfriday" in source["ics_url"]

    large = _call(client, "get_calendar_link", {
        "filters": {
            "categories": ["music"],
            "participant_count_min": 300,
            "min_scale_confidence": 0.3,
            "required_attributes": ["event_scale"],
        },
    })
    assert "participant_count_min=300" in large["ics_url"]
    assert "min_scale_confidence=0.3" in large["ics_url"]


def test_calendar_link_proves_highlighted_occurrence_coverage(conn, client):
    occurrence_id = conn.execute(
        "SELECT o.id FROM occurrence o JOIN event e ON e.id = o.event_id "
        "WHERE e.title = 'Salsa Social'"
    ).fetchone()["id"]

    covered = _call(client, "get_calendar_link", {
        "filters": {"categories": ["nightlife"]},
        "accepted_occurrence_ids": [str(occurrence_id)],
    })

    assert covered["coverage_complete"] is True
    assert covered["coverage"] == [{
        "occurrence_id": str(occurrence_id),
        "title": "Salsa Social",
        "included": True,
        "reasons": [],
    }]
    assert "limit=1000" in covered["ics_url"]
    feed_url = urlsplit(covered["ics_url"])
    rendered = client.get(f"{feed_url.path}?{feed_url.query}")
    assert f"UID:{occurrence_id}@eventindex".encode() in rendered.content

    omitted = _call(client, "get_calendar_link", {
        "filters": {"categories": ["music"]},
        "accepted_occurrence_ids": [str(occurrence_id)],
    })
    assert omitted["coverage_complete"] is False
    assert omitted["ics_url"] is None
    assert "category" in omitted["coverage"][0]["reasons"]


def test_calendar_link_explains_time_geo_and_confidence_omissions(conn, client):
    row = conn.execute(
        "SELECT o.id, e.id AS event_id FROM occurrence o "
        "JOIN event e ON e.id = o.event_id WHERE e.title = 'Salsa Social'"
    ).fetchone()
    conn.execute(
        "UPDATE occurrence SET time_unknown = true WHERE id = %s", (row["id"],)
    )
    conn.execute(
        "UPDATE event SET confidence = 0.2 WHERE id = %s", (row["event_id"],)
    )
    conn.commit()

    out = _call(client, "get_calendar_link", {
        "filters": {
            "categories": ["nightlife"],
            "near": "47.0,13.0",
            "radius": "1km",
        },
        "accepted_occurrence_ids": [str(row["id"])],
    })

    assert out["ics_url"] is None
    assert set(out["coverage"][0]["reasons"]) >= {
        "time_unknown", "geography", "confidence",
    }


def test_calendar_link_serializes_geo_and_rejects_unpreservable_filters(client):
    out = _call(client, "get_calendar_link", {
        "filters": {
            "categories": ["nightlife"],
            "near": "48.3069,14.2858",
            "radius": "3km",
        },
    })
    assert "near=48.3069%2C14.2858" in out["ics_url"]
    assert "radius=3km" in out["ics_url"]

    for filters, expected in (
        ({"categories": ["nightlife"], "age_min": 20}, "age"),
        (
            {"categories": ["nightlife"], "sex_service_context": True},
            "sex_service_context=true",
        ),
    ):
        body = _rpc(client, "tools/call", {
            "name": "get_calendar_link", "arguments": {"filters": filters},
        })
        assert body["result"].get("isError") is True
        assert expected in body["result"]["content"][0]["text"].lower()


def test_calendar_radius_any_overrides_near_and_preserves_unknown_geo(
    conn, client,
):
    occurrence_id = conn.execute(
        "SELECT o.id FROM occurrence o JOIN event e ON e.id = o.event_id "
        "WHERE e.title = 'Chamber Concert'"
    ).fetchone()["id"]

    out = _call(client, "get_calendar_link", {
        "filters": {
            "categories": ["music"],
            "near": "47.0,13.0",
            "radius": "any",
        },
        "accepted_occurrence_ids": [str(occurrence_id)],
    })

    assert out["coverage_complete"] is True
    assert out["coverage"][0]["included"] is True
    assert "radius=any" in out["ics_url"]


def test_calendar_coverage_reports_non_scheduled_and_missing_ids(conn, client):
    row = conn.execute(
        "SELECT o.id FROM occurrence o JOIN event e ON e.id = o.event_id "
        "WHERE e.title = 'Salsa Social'"
    ).fetchone()
    conn.execute(
        "UPDATE occurrence SET status = 'moved' WHERE id = %s", (row["id"],)
    )
    conn.commit()

    out = _call(client, "get_calendar_link", {
        "filters": {"categories": ["nightlife"]},
        "accepted_occurrence_ids": [str(row["id"]), str(uuid.uuid4())],
    })

    assert out["ics_url"] is None
    assert out["coverage_complete"] is False
    assert out["coverage"][0]["reasons"] == ["not_scheduled"]
    assert out["coverage"][1]["reasons"] == ["not_found"]


def test_calendar_coverage_does_not_reveal_safety_suppressed_title(conn, client):
    row = conn.execute(
        "SELECT o.id, e.id AS event_id FROM occurrence o "
        "JOIN event e ON e.id = o.event_id WHERE e.title = 'Salsa Social'"
    ).fetchone()
    _mark_sex_service(conn, row["event_id"])
    conn.commit()

    out = _call(client, "get_calendar_link", {
        "filters": {"categories": ["nightlife"]},
        "accepted_occurrence_ids": [str(row["id"])],
    })

    assert out["ics_url"] is None
    assert out["coverage_complete"] is False
    assert out["coverage"] == [{
        "occurrence_id": str(row["id"]),
        "title": None,
        "included": False,
        "reasons": ["sex_service_safety"],
    }]


def test_mcp_tools_never_expose_audience_unready_events(conn, client):
    event_id = _add_event(
        conn, "Audience Private Run", starts=NOW + timedelta(days=1),
        lat=48.30, lon=14.29, category=["sport"],
    )
    occurrence_id = conn.execute(
        "SELECT id FROM occurrence WHERE event_id = %s", (event_id,)
    ).fetchone()["id"]
    conn.execute(
        "UPDATE event SET inferred = inferred - '_audience_essentials' "
        "WHERE id = %s",
        (event_id,),
    )
    conn.commit()

    discovered = _call(client, "search_events", {
        "filters": {
            "name": "Audience Private Run", "min_confidence": 0,
            "radius": "any",
        },
        "limit": 10,
    })
    assert discovered["occurrences"] == []
    assert _call(client, "search", {
        "query": "Audience Private Run",
    })["results"] == []
    for tool, arguments in (
        ("get_event", {"event_id": str(event_id)}),
        ("fetch", {"id": str(event_id)}),
    ):
        body = _rpc(client, "tools/call", {
            "name": tool, "arguments": arguments,
        })
        assert body["result"]["isError"] is True

    calendar = _call(client, "get_calendar_link", {
        "filters": {"categories": ["sport"]},
        "accepted_occurrence_ids": [str(occurrence_id)],
    })
    assert calendar["ics_url"] is None
    assert calendar["coverage"] == [{
        "occurrence_id": str(occurrence_id),
        "title": None,
        "included": False,
        "reasons": ["audience_ready"],
    }]


def test_calendar_coverage_caps_accepted_occurrence_ids(client):
    body = _rpc(client, "tools/call", {
        "name": "get_calendar_link",
        "arguments": {
            "filters": {"categories": ["music"]},
            "accepted_occurrence_ids": [str(uuid.uuid4()) for _ in range(101)],
        },
    })
    assert body["result"].get("isError") is True


def test_calendar_coverage_reports_a_weak_requested_concept(
    conn, client, monkeypatch,
):
    import numpy as np

    from eventindex import embeddings, tags

    event_id = _add_event(
        conn, "Sound Session", starts=NOW + timedelta(days=3),
        category=["learning"],
    )
    tags.upsert(conn, event_id, "singing", 0.8, "inferred")
    tags.upsert(conn, event_id, "movement", 0.2, "inferred")
    occurrence_id = conn.execute(
        "SELECT id FROM occurrence WHERE event_id = %s", (event_id,)
    ).fetchone()["id"]
    conn.commit()
    monkeypatch.setattr(
        embeddings,
        "embed_tags",
        lambda values: np.zeros(
            (len(values), embeddings.DIMENSIONS), dtype=np.float32
        ),
    )

    out = _call(client, "get_calendar_link", {
        "filters": {
            "tags": ["singing", "movement"],
            "min_tag_match": 0.25,
            "min_tag_concept_match": 0.3,
        },
        "accepted_occurrence_ids": [str(occurrence_id)],
    })

    assert out["ics_url"] is None
    assert out["coverage"][0]["reasons"] == ["tag_concept_match"]


def test_get_calendar_link_rejects_an_unscoped_subscription(client):
    body = _rpc(client, "tools/call", {
        "name": "get_calendar_link", "arguments": {},
    })
    assert body["result"].get("isError") is True
    message = body["result"]["content"][0]["text"].lower()
    assert "categories" in message and "tags" in message


def test_get_calendar_link_requires_explicit_semantic_membership(client):
    body = _rpc(client, "tools/call", {
        "name": "get_calendar_link",
        "arguments": {"filters": {"source": "WKO", "tags": ["startup"]}},
    })
    assert body["result"].get("isError") is True
    message = body["result"]["content"][0]["text"].lower()
    assert "min_tag_match" in message
    assert "weakest accepted result" in message


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
        "UPDATE event SET venue_id = %s WHERE id = %s",
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


def test_safety_exclusion_does_not_become_a_hidden_ranking_preference(
    conn, client,
):
    known_safe = _add_event(
        conn, "Safety Ranking Known",
        starts=NOW + timedelta(days=1), lat=48.30, lon=14.29,
        category=["community"],
    )
    unknown = _add_event(
        conn, "Safety Ranking Unknown",
        starts=NOW + timedelta(days=2), lat=48.30, lon=14.29,
        category=["community"],
    )
    conn.execute(
        "UPDATE event SET confidence = 0.4, inferred = inferred || %s "
        "WHERE id = %s",
        (Jsonb({"sex_service_context": {
            "value": False, "confidence": 0.8, "evidence": "ordinary venue",
        }}), known_safe),
    )
    conn.execute(
        "UPDATE event SET confidence = 0.95 WHERE id = %s",
        (unknown,),
    )
    conn.commit()

    out = _call(client, "search_events", {
        "filters": {
            "name": "Safety Ranking",
            "sex_service_context": False,
        },
        "limit": 10,
    })
    assert [uuid.UUID(row["event_id"]) for row in out["occurrences"]] == [
        unknown, known_safe,
    ]


def test_standard_search_is_hard_relevant_future_and_distinct(conn, client):
    future_id = _add_event(
        conn, "HWYD Social Run", starts=NOW + timedelta(days=3),
        lat=48.30, lon=14.29, category=["sport"],
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s)",
        (future_id, NOW + timedelta(days=4)),
    )
    ongoing_id = _add_event(
        conn, "Ongoing Run Exhibition", starts=NOW - timedelta(days=2),
        category=["sport"],
    )
    conn.execute(
        "UPDATE occurrence SET ends_at = %s WHERE event_id = %s",
        (NOW + timedelta(days=2), ongoing_id),
    )
    polluted_id = _add_event(
        conn, "Football Practice", starts=NOW + timedelta(days=2),
        lat=48.30, lon=14.29, category=["sport"],
    )
    conn.execute(
        "INSERT INTO event_tag (event_id, name, confidence, origins) "
        "VALUES (%s, 'run', 0.8, '{inferred}')",
        (polluted_id,),
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

    results = _call(client, "search", {"query": "social run"})["results"]
    ids = [uuid.UUID(result["id"]) for result in results]
    # ranked OR: the double hit outranks everything, appears exactly once
    assert ids[0] == future_id
    assert ids.count(future_id) == 1
    assert ongoing_id not in ids       # past-start stays excluded
    assert polluted_id not in ids      # semantic tags are not name-search evidence
    phrase_results = _call(client, "search", {
        "query": "football lounge nights special",
    })["results"]
    phrase_ids = {uuid.UUID(result["id"]) for result in phrase_results}
    assert exact_phrase_id in phrase_ids
    assert filler_id not in phrase_ids  # fail-closed: no single-word filler


def test_exact_entity_search_finds_and_labels_tentative_alphanumeric_names(
        conn, client):
    venue_id = conn.execute(
        "INSERT INTO venue (name) VALUES ('factory300') RETURNING id"
    ).fetchone()["id"]
    factory_id = _add_event(
        conn, "Community Lunch", starts=NOW + timedelta(days=2),
        category=["community"],
    )
    conn.execute(
        "UPDATE event SET venue_id = %s, organizer = 'factory300', "
        "confidence = 0.3 WHERE id = %s",
        (venue_id, factory_id),
    )
    data_factory_id = _add_event(
        conn, "Microsoft Data Factory Training",
        starts=NOW + timedelta(days=1),
        category=["learning"],
    )
    conn.commit()

    exact = _call(client, "search", {"query": "factory300"})
    assert [uuid.UUID(row["id"]) for row in exact["results"]] == [factory_id]
    assert "tentative confidence 0.30" in exact["results"][0]["title"]
    assert "present in the index" in exact["hint"]
    assert data_factory_id not in {
        uuid.UUID(row["id"]) for row in exact["results"]
    }

    hidden = _call(client, "search_events", {
        "filters": {"venue": "factory300"},
        "limit": 10,
    })
    assert hidden["occurrences"] == []
    assert "1 lower-confidence match exists" in hidden["diagnostics"]["message"]
    assert "min_confidence=0" in hidden["diagnostics"]["suggested_retry"]

    transparent = _call(client, "search_events", {
        "filters": {"venue": "factory300", "min_confidence": 0},
        "limit": 10,
    })
    assert [uuid.UUID(row["event_id"]) for row in transparent["occurrences"]] == [
        factory_id
    ]

    cached_client = _call(client, "search_events", {
        "filters": {"include_terms": ["factory300"]},
        "limit": 10,
    })
    assert cached_client["parsed_filters"]["min_confidence"] == 0
    assert [
        uuid.UUID(row["event_id"]) for row in cached_client["occurrences"]
    ] == [factory_id]


def test_search_events_places_in_window_starts_before_ongoing(conn, client):
    ongoing_id = _add_event(
        conn, "Long Exhibition", starts=NOW - timedelta(days=5),
        category=["art"],
    )
    conn.execute(
        "UPDATE occurrence SET ends_at = %s WHERE event_id = %s",
        (NOW + timedelta(days=5), ongoing_id),
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
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO event_tag (event_id, name, confidence, origins) "
            "VALUES (%s, %s, 0.4, '{inferred}')",
            [(event_id, f"tag-{i}") for i in range(20)],
        )
    conn.commit()

    mcp_detail = _call(client, "get_event", {"event_id": str(event_id)})
    assert "SECRET PRIVATE ADDRESS" not in json.dumps(mcp_detail)
    assert mcp_detail["sources"][0]["name"] == "Public Source"
    assert mcp_detail["sources"][0]["url"] == \
        "https://source.example/events/correct-event"
    assert len(mcp_detail["event"]["tags"]) == 16
    assert set(mcp_detail["event"]["tags"][0]) == {
        "name", "confidence", "origins", "evidence_bases",
    }
    assert mcp_detail["event"]["tags"][0]["evidence_bases"] == ["unknown"]
    public_detail = client.get(f"/v1/events/{event_id}").json()
    assert "claims" not in public_detail
    assert "SECRET PRIVATE ADDRESS" not in json.dumps(public_detail)


def test_search_handles_natural_german_queries(conn, client):
    konzert_id = _add_event(conn, "Gartenkonzert der Stadtkapelle",
                            starts=NOW + timedelta(days=2), category=["music"])
    markt_id = _add_event(conn, "Keramikmarkt am Hauptplatz",
                          starts=NOW + timedelta(days=2), category=["culture"])
    venue_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO venue (id, name, address) VALUES (%s, %s, %s)",
        (venue_id, "Donaupark", "Untere Donaulände 7, 4020 Linz"),
    )
    conn.execute("UPDATE event SET venue_id = %s WHERE id IN (%s, %s)",
                 (venue_id, konzert_id, markt_id))
    conn.commit()

    results = _call(client, "search", {
        "query": "Konzerte am Wochenende in Linz",
    })["results"]
    ids = [uuid.UUID(result["id"]) for result in results]
    assert ids[0] == konzert_id  # plural + compound + location all absorbed


def test_search_returns_hint_when_nothing_matches(client):
    out = _call(client, "search", {"query": "Quantenknödelfestival übermorgen"})
    assert out["results"] == []
    assert "search_events" in out["hint"]


def test_search_labels_unknown_times_instead_of_midnight(conn, client):
    event_id = _add_event(conn, "Sommerfest im Park",
                          starts=NOW + timedelta(days=2), category=["culture"])
    conn.execute(
        "UPDATE occurrence SET time_unknown = true WHERE event_id = %s",
        (event_id,),
    )
    conn.commit()
    results = _call(client, "search", {"query": "Sommerfest"})["results"]
    title = next(r["title"] for r in results
                 if uuid.UUID(r["id"]) == event_id)
    assert "(time unknown)" in title
    assert "00:00" not in title


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
