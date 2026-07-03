"""API filter semantics - the null=unknown contract and keyset pagination."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

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


def test_default_excludes_past(client):
    titles = _titles(client.get("/v1/occurrences"))
    assert "Already Happened" not in titles
    assert len(titles) == 4


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
    assert len(seen) == len(set(seen)) == 4


def test_event_detail_404(client):
    assert client.get(f"/v1/events/{uuid.uuid4()}").status_code == 404
