"""API filter semantics - the null=unknown contract and keyset pagination."""

import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from icalendar import Calendar
import pytest
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from eventindex import config
from eventindex.api.app import app

NOW = datetime.now(timezone.utc)


def _add_event(conn, title, *, starts, lat=None, lon=None, category=None):
    event_id = uuid.uuid4()
    inferred = Jsonb({
        "energy": "medium",
        "solo_friendly": {"value": True, "confidence": 0.5},
        "_audience_essentials": {
            "energy": {"value": "medium", "confidence": 0.5},
        },
    })
    conn.execute(
        """
        INSERT INTO event (
            id, kind, title, category, geo, confidence, status, inferred,
            expected_gender_split, expected_gender_split_confidence
        )
        VALUES (%(id)s, 'one_off', %(title)s, %(cats)s,
                CASE WHEN %(lat)s::float IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
                0.9, 'confirmed', %(inferred)s, 0.5, 0.7)
        """,
        {
            "id": event_id, "title": title, "cats": category or [],
            "lat": lat, "lon": lon, "inferred": inferred,
        },
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


def test_low_confidence_scale_never_presents_an_exact_range():
    from eventindex.api.app import _public_event_scale

    scale = _public_event_scale({
        "expected_attendance": 50,
        "expected_attendance_confidence": 0.2,
        "inferred": {},
    })
    assert scale["estimated_participants"] == 50
    assert scale["plausible_min"] == 30
    assert scale["plausible_max"] == 70
    assert scale["confidence"] == 0.2
    assert scale["estimate_status"] == "estimated"
    assert _public_event_scale({"inferred": {}})["estimate_status"] == "unknown"

    explicit = _public_event_scale({
        "expected_attendance": 50,
        "expected_attendance_confidence": 0.2,
        "inferred": {
            "event_scale": {
                "estimated_participants": 50,
                "plausible_min": 20,
                "plausible_max": 120,
                "confidence": 0.2,
                "basis": ["venue"],
            },
        },
    })
    assert (explicit["plausible_min"], explicit["plausible_max"]) == (20, 120)


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
    # `any` is a true opt-out even if a stale caller also sends `near`.
    any_near_titles = _titles(client.get(
        "/v1/occurrences",
        params={"near": "48.3069,14.2858", "radius": "any"},
    ))
    assert set(any_near_titles) == set(all_titles)


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


def test_occurrence_summary_uses_vienna_calendar_ranges_and_listing_policy(
    conn, client, monkeypatch,
):
    from eventindex.api import app as app_mod

    vienna = ZoneInfo("Europe/Vienna")
    fixed_now = datetime(2026, 8, 12, 14, 30, tzinfo=vienna)  # Wednesday

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                fixed_now.replace(tzinfo=None)
                if tz is None else fixed_now.astimezone(tz)
            )

    monkeypatch.setattr(app_mod, "datetime", FrozenDateTime)

    def local(day, hour=12, minute=0):
        return datetime(2026, 8, day, hour, minute, tzinfo=vienna)

    def add(title, starts, *, ends=None, lat=48.3069, lon=14.2858):
        event_id = _add_event(
            conn, title, starts=starts, lat=lat, lon=lon,
            category=["culture"],
        )
        if ends is not None:
            conn.execute(
                "UPDATE occurrence SET ends_at = %s WHERE event_id = %s",
                (ends, event_id),
            )
        return event_id

    add("Today boundary", local(12, 0))
    add("Monday", local(10))
    add("Sunday", local(16, 23, 59))
    add("Next Monday boundary", local(17, 0))
    add(
        "Last day of 30", datetime(2026, 9, 10, 23, 59, tzinfo=vienna),
    )
    add(
        "Outside 30", datetime(2026, 9, 11, 0, 0, tzinfo=vienna),
    )
    add("Ongoing", local(1), ends=local(12, 15))
    add("Ended before week", local(1), ends=local(9, 23, 59))
    moved = add("Moved", local(12, 16))
    conn.execute(
        "UPDATE occurrence SET status = 'moved' WHERE event_id = %s", (moved,),
    )
    low_confidence = add("Low confidence", local(12, 17))
    conn.execute(
        "UPDATE event SET confidence = 0.39 WHERE id = %s", (low_confidence,),
    )
    add("Far away", local(12, 18), lat=48.15, lon=14.03)
    add("Unknown location", local(12, 19), lat=None, lon=None)
    conn.commit()

    response = client.get(
        "/v1/occurrences/summary", params={"category": "culture"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["timezone"] == "Europe/Vienna"
    assert datetime.fromisoformat(body["as_of"]) == fixed_now
    assert {
        name: window["count"] for name, window in body["ranges"].items()
    } == {"today": 3, "this_week": 5, "next_30_days": 6}

    expected = {
        "today": (
            local(12, 0),
            datetime(2026, 8, 12, 23, 59, 59, 999999, tzinfo=vienna),
        ),
        "this_week": (
            local(10, 0),
            datetime(2026, 8, 16, 23, 59, 59, 999999, tzinfo=vienna),
        ),
        "next_30_days": (
            local(12, 0),
            datetime(2026, 9, 10, 23, 59, 59, 999999, tzinfo=vienna),
        ),
    }
    for name, (from_, to) in expected.items():
        assert datetime.fromisoformat(body["ranges"][name]["from"]) == from_
        assert datetime.fromisoformat(body["ranges"][name]["to"]) == to


def test_occurrence_summary_documents_only_real_browse_filters(client):
    operation = client.get("/openapi.json").json()["paths"][
        "/v1/occurrences/summary"
    ]["get"]
    assert [parameter["name"] for parameter in operation["parameters"]] == [
        "category", "is_free", "solo_friendly", "gender_split_min",
        "gender_split_max", "energy", "gender_split_band",
    ]


def test_browse_estimate_filters_keep_summary_cursor_and_rows_in_sync(
    conn, client, monkeypatch,
):
    from eventindex.api import app as app_mod

    vienna = ZoneInfo("Europe/Vienna")
    fixed_now = datetime(2026, 8, 14, 10, 0, tzinfo=vienna)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                fixed_now.replace(tzinfo=None)
                if tz is None else fixed_now.astimezone(tz)
            )

    monkeypatch.setattr(app_mod, "datetime", FrozenDateTime)

    def add(
        title, *, price, solo, gender, energy,
    ):
        event_id = _add_event(
            conn, title,
            starts=datetime(2026, 8, 14, 12, tzinfo=vienna),
            lat=48.3069, lon=14.2858, category=["tech"],
        )
        inferred = {}
        if solo is not None:
            inferred["solo_friendly"] = {
                "value": solo, "confidence": 0.72,
            }
        if energy is not None:
            inferred["energy"] = energy
            inferred["_audience_essentials"] = {
                "energy": {"value": energy, "confidence": 0.66},
            }
        conn.execute(
            "UPDATE event SET price_min = %s, price_max = %s, inferred = %s, "
            "expected_gender_split = %s, "
            "expected_gender_split_confidence = %s WHERE id = %s",
            (
                price, price, Jsonb(inferred), gender,
                0.68 if gender is not None else None, event_id,
            ),
        )
        return event_id

    matching = add(
        "Matching", price=0, solo=True, gender=0.65, energy="high",
    )
    add("Paid", price=15, solo=True, gender=0.65, energy="high")
    add("Not solo", price=0, solo=False, gender=0.65, energy="high")
    add("Unknown estimates", price=0, solo=None, gender=None, energy=None)
    add("Low energy", price=0, solo=True, gender=0.65, energy="low")
    add("Male leaning", price=0, solo=True, gender=0.2, energy="high")
    for name, confidence, origins in (
        ("tech", 0.99, ["category"]),
        ("live music", 0.92, ["source"]),
        ("dancing", 0.81, ["inferred"]),
        ("social", 0.65, ["inferred"]),
        ("night event", 0.49, ["inferred"]),
    ):
        conn.execute(
            "INSERT INTO event_tag "
            "(event_id, name, confidence, origins, origin_confidences) "
            "VALUES (%s, %s, %s, %s, %s)",
            (
                matching, name, confidence, origins,
                Jsonb({origin: confidence for origin in origins}),
            ),
        )
    conn.commit()

    filter_sets = [
        {"is_free": "true"},
        {"solo_friendly": "true"},
        {"gender_split_min": 0.6, "gender_split_max": 0.8},
        {"gender_split_band": "high"},
        {"energy": "high"},
        {
            "is_free": "true", "solo_friendly": "true",
            "gender_split_min": 0.6, "gender_split_max": 0.8,
            "energy": "high",
        },
    ]
    for filters in filter_sets:
        summary = client.get(
            "/v1/occurrences/summary",
            params={"category": "tech", **filters},
        ).json()["ranges"]["today"]
        cursor = None
        rows = []
        while True:
            params = {
                "category": "tech", "from": summary["from"],
                "to": summary["to"], "limit": 2, **filters,
            }
            if cursor:
                params["cursor"] = cursor
            body = client.get("/v1/occurrences", params=params).json()
            rows.extend(body["occurrences"])
            cursor = body["next_cursor"]
            if cursor is None:
                break
        assert summary["count"] == len(rows)
        assert len({row["id"] for row in rows}) == len(rows)

    combo = client.get("/v1/occurrences", params={
        "category": "tech", "from": "2026-08-14T00:00:00+02:00",
        "to": "2026-08-14T23:59:59+02:00", "is_free": "true",
        "solo_friendly": "true", "gender_split_min": 0.6,
        "gender_split_max": 0.8, "energy": "high",
    }).json()["occurrences"]
    assert [row["title"] for row in combo] == ["Matching"]
    assert combo[0]["estimates"] == {
        "solo_friendly": {"value": True, "confidence": 0.72},
        "gender_split": {"value": 0.65, "confidence": 0.68},
        "energy": {"value": "high", "confidence": 0.66},
    }
    assert [tag["name"] for tag in combo[0]["tags"]] == [
        "live music", "dancing", "social", "night event",
    ]
    assert combo[0]["tags"][0]["origins"] == ["source"]

    # false keeps the established no-restriction meaning for is_free.
    unfiltered = client.get("/v1/occurrences", params={
        "category": "tech", "from": "2026-08-14T00:00:00+02:00",
        "to": "2026-08-14T23:59:59+02:00", "is_free": "false",
    }).json()["occurrences"]
    assert len(unfiltered) == 5

    for path in ("/v1/occurrences", "/v1/occurrences/summary"):
        assert client.get(path, params={
            "gender_split_min": 0.8, "gender_split_max": 0.2,
        }).status_code == 422
        assert client.get(path, params={"gender_split_min": 1.1}).status_code == 422
        assert client.get(path, params={"energy": "extreme"}).status_code == 422
        assert client.get(
            path, params={"gender_split_band": "unknown"},
        ).status_code == 422


def test_public_ready_set_is_exactly_partitioned_in_every_calendar_range(
    conn, client, monkeypatch,
):
    from eventindex.api import app as app_mod

    vienna = ZoneInfo("Europe/Vienna")
    fixed_now = datetime(2026, 8, 14, 10, 0, tzinfo=vienna)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                fixed_now.replace(tzinfo=None)
                if tz is None else fixed_now.astimezone(tz)
            )

    monkeypatch.setattr(app_mod, "datetime", FrozenDateTime)

    def add(title, starts, *, energy, gender, solo=True):
        event_id = _add_event(
            conn, title, starts=starts, lat=48.3069, lon=14.2858,
            category=["sport"],
        )
        inferred = {
            "solo_friendly": {"value": solo, "confidence": 0.6},
        }
        if energy is not None:
            inferred |= {
                "energy": energy,
                "_audience_essentials": {
                    "energy": {"value": energy, "confidence": 0.6},
                },
            }
        conn.execute(
            "UPDATE event SET inferred = %s, expected_gender_split = %s, "
            "expected_gender_split_confidence = %s "
            "WHERE id = %s",
            (Jsonb(inferred), gender, 0.6 if gender is not None else None, event_id),
        )

    slots = (
        ("today", datetime(2026, 8, 14, 12, tzinfo=vienna)),
        ("week", datetime(2026, 8, 15, 12, tzinfo=vienna)),
        ("month", datetime(2026, 8, 20, 12, tzinfo=vienna)),
    )
    balanced_values = (0.4, 0.6, 0.5)
    for index, (slot, starts) in enumerate(slots):
        add(f"{slot} low", starts, energy="low", gender=0.39, solo=True)
        add(
            f"{slot} balanced", starts, energy="medium",
            gender=balanced_values[index], solo=False,
        )
        add(f"{slot} high", starts, energy="high", gender=0.61, solo=True)

    # Incomplete or invalid estimates are not silently assigned to a bucket;
    # they stay outside the public-ready chronological calendar.
    today = slots[0][1]
    add("missing energy", today, energy=None, gender=0.5)
    add("missing gender", today, energy="low", gender=None)
    add("invalid energy", today, energy="extreme", gender=0.5)
    add("gender below domain", today, energy="low", gender=-0.1)
    add("gender above domain", today, energy="high", gender=1.1)
    conn.commit()

    filter_sets = {
        "all": {},
        "energy_low": {"energy": "low"},
        "energy_medium": {"energy": "medium"},
        "energy_high": {"energy": "high"},
        "gender_low": {"gender_split_band": "low"},
        "gender_balanced": {"gender_split_band": "balanced"},
        "gender_high": {"gender_split_band": "high"},
        "solo_true": {"solo_friendly": "true"},
        "solo_false": {"solo_friendly": "false"},
    }
    counts = {label: {} for label in filter_sets}
    listed = {label: {} for label in filter_sets}

    for label, filters in filter_sets.items():
        summary_response = client.get(
            "/v1/occurrences/summary",
            params={"category": "sport", **filters},
        )
        assert summary_response.status_code == 200
        ranges = summary_response.json()["ranges"]
        for range_name, window in ranges.items():
            cursor = None
            rows = []
            while True:
                params = {
                    "category": "sport", "from": window["from"],
                    "to": window["to"], "limit": 2, **filters,
                }
                if cursor is not None:
                    params["cursor"] = cursor
                listing_response = client.get(
                    "/v1/occurrences", params=params,
                )
                assert listing_response.status_code == 200
                page = listing_response.json()
                rows.extend(page["occurrences"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
            ids = [row["id"] for row in rows]
            assert len(ids) == len(set(ids)) == window["count"]
            counts[label][range_name] = window["count"]
            listed[label][range_name] = rows

    assert [counts["all"][name] for name in (
        "today", "this_week", "next_30_days",
    )] == [3, 6, 9]
    assert {row["title"] for row in listed["all"]["today"]} == {
        "today low", "today balanced", "today high",
    }
    for range_name in ("today", "this_week", "next_30_days"):
        assert counts["all"][range_name] == sum(
            counts[f"energy_{bucket}"][range_name]
            for bucket in ("low", "medium", "high")
        )
        assert counts["all"][range_name] == sum(
            counts[f"gender_{bucket}"][range_name]
            for bucket in ("low", "balanced", "high")
        )
        assert counts["all"][range_name] == sum(
            counts[f"solo_{value}"][range_name]
            for value in ("true", "false")
        )


def test_audience_publication_gate_covers_every_public_read_surface(
    conn, client, monkeypatch,
):
    """Neither a pending row nor corrupt mandatory estimates may leak."""
    from eventindex.api import app as app_mod
    from eventindex.api import search as search_mod

    hidden_id = _add_event(
        conn, "Audience Gate Hidden", starts=NOW + timedelta(days=1),
        lat=48.3069, lon=14.2858, category=["sport"],
    )
    hidden_occurrence = conn.execute(
        "SELECT id FROM occurrence WHERE event_id = %s", (hidden_id,)
    ).fetchone()["id"]
    conn.execute(
        "UPDATE event SET inferred = inferred - '_audience_essentials' "
        "WHERE id = %s",
        (hidden_id,),
    )

    pending_id = _add_event(
        conn, "Audience Gate Pending", starts=NOW + timedelta(days=2),
        lat=48.3069, lon=14.2858, category=["sport"],
    )
    pending_occurrence = conn.execute(
        "SELECT id FROM occurrence WHERE event_id = %s", (pending_id,)
    ).fetchone()["id"]
    conn.execute(
        "UPDATE occurrence SET status = 'pending_enrichment' WHERE id = %s",
        (pending_occurrence,),
    )
    conn.commit()

    browse_params = {"category": "sport", "radius": "any", "min_confidence": 0}
    assert client.get(
        "/v1/occurrences", params=browse_params,
    ).json()["occurrences"] == []
    assert client.get(
        "/v1/occurrences/summary", params={"category": "sport"},
    ).json()["ranges"]["next_30_days"]["count"] == 0
    feed = client.get("/v1/feed.ics", params=browse_params)
    assert b"Audience Gate Hidden" not in feed.content
    assert b"Audience Gate Pending" not in feed.content
    assert client.post(
        "/v1/query?limit=100", json={
            "categories": ["sport"], "radius": "any", "min_confidence": 0,
        },
    ).json()["occurrences"] == []

    parsed = search_mod.SearchFilters(**(
        search_mod.FILTER_DEFAULTS
        | {"categories": ["sport"], "radius": "any", "min_confidence": 0}
    ))
    monkeypatch.setattr(search_mod, "parse_query", lambda *_: parsed)
    assert client.get(
        "/v1/search", params={"q": "sport", "limit": 100},
    ).json()["occurrences"] == []

    changes = client.get("/v1/changes", params={"limit": 500}).json()["events"]
    changed_ids = {uuid.UUID(row["id"]) for row in changes}
    assert hidden_id not in changed_ids
    assert pending_id not in changed_ids
    assert client.get(f"/v1/events/{hidden_id}").status_code == 404
    assert client.get(f"/v1/events/{pending_id}").status_code == 404

    coverage = app_mod._feed_coverage(
        [hidden_occurrence, pending_occurrence], from_=NOW, radius="any",
        category="sport", min_confidence=0,
        include_time_unknown=True, limit=10,
    )
    assert coverage[0]["included"] is False
    assert coverage[0]["title"] is None
    assert "audience_ready" in coverage[0]["reasons"]
    assert coverage[1] == {
        "occurrence_id": pending_occurrence,
        "title": None,
        "included": False,
        "reasons": ["not_scheduled"],
    }


def test_publication_gate_validates_values_and_positive_confidence(conn, client):
    def audience(*, energy="medium", energy_meta="medium", energy_conf=0.5,
                 solo=True, solo_conf=0.5):
        inferred = {
            "energy": energy,
            "_audience_essentials": {
                "energy": {"value": energy_meta, "confidence": energy_conf},
            },
        }
        if solo is not None:
            inferred["solo_friendly"] = {
                "value": solo, "confidence": solo_conf,
            }
        return inferred

    def add(title, *, inferred, gender=0.5, gender_conf=0.5):
        event_id = _add_event(
            conn, title, starts=NOW + timedelta(days=1),
            lat=48.3069, lon=14.2858, category=["community"],
        )
        conn.execute(
            "UPDATE event SET inferred = %s, expected_gender_split = %s, "
            "expected_gender_split_confidence = %s WHERE id = %s",
            (Jsonb(inferred), gender, gender_conf, event_id),
        )

    add("Ready control", inferred=audience())
    add("Zero gender confidence", inferred=audience(), gender_conf=0)
    add("Zero energy confidence", inferred=audience(energy_conf=0))
    add("Mismatched energy metadata", inferred=audience(energy_meta="high"))
    add("Missing solo value", inferred=audience(solo=None))
    add("Zero solo confidence", inferred=audience(solo_conf=0))
    add("Invalid solo type", inferred=audience(solo="definitely"))
    conn.commit()

    for extra_params in ({}, {"solo_friendly": "true"}):
        response = client.get("/v1/occurrences", params={
            "category": "community", "radius": "any", "min_confidence": 0,
            **extra_params,
        })
        assert response.status_code == 200
        assert [
            row["title"] for row in response.json()["occurrences"]
        ] == ["Ready control"]


@pytest.mark.parametrize(
    ("local_now", "day_hours"),
    [
        (datetime(2026, 3, 29, 12, tzinfo=ZoneInfo("Europe/Vienna")), 23),
        (datetime(2026, 10, 25, 12, tzinfo=ZoneInfo("Europe/Vienna")), 25),
    ],
)
def test_occurrence_summary_keeps_vienna_midnights_across_dst(
    client, monkeypatch, local_now, day_hours,
):
    from eventindex.api import app as app_mod

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return local_now.replace(tzinfo=None) if tz is None else local_now.astimezone(tz)

    monkeypatch.setattr(app_mod, "datetime", FrozenDateTime)
    today = client.get("/v1/occurrences/summary").json()["ranges"]["today"]
    start = datetime.fromisoformat(today["from"])
    exclusive_end = datetime.fromisoformat(today["to"]) + timedelta(microseconds=1)

    assert (start.hour, start.minute, exclusive_end.hour, exclusive_end.minute) == (
        0, 0, 0, 0,
    )
    assert (exclusive_end.astimezone(timezone.utc) - start.astimezone(timezone.utc)) == (
        timedelta(hours=day_hours)
    )


def test_occurrence_summary_is_exact_beyond_one_listing_page(conn, client):
    vienna = ZoneInfo("Europe/Vienna")
    starts = datetime.now(vienna).replace(hour=12, minute=0, second=0, microsecond=0)
    for index in range(205):
        _add_event(
            conn, f"Tech Termin {index:03d}", starts=starts,
            lat=48.3069, lon=14.2858, category=["tech"],
        )
    conn.commit()

    summary = client.get(
        "/v1/occurrences/summary", params={"category": "tech"},
    ).json()["ranges"]["today"]
    seen = []
    cursor = None
    while True:
        params = {
            "from": summary["from"], "to": summary["to"],
            "category": "tech", "limit": 200,
        }
        if cursor is not None:
            params["cursor"] = cursor
        body = client.get("/v1/occurrences", params=params).json()
        seen.extend(row["id"] for row in body["occurrences"])
        cursor = body["next_cursor"]
        if cursor is None:
            break

    assert summary["count"] == len(seen) == len(set(seen)) == 205


def test_calendar_gui_is_browse_first_and_uses_only_real_query_surfaces(client):
    response = client.get("/calendar")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text

    assert "{categories_json}" not in html
    assert 'lang="de"' in html
    assert '/v1/occurrences/summary' in html
    assert '/v1/occurrences?' in html
    assert '/v1/feed.ics?' in html
    assert 'data-window="today"' in html
    assert 'data-window="this_week"' in html
    assert 'data-window="next_30_days"' in html
    assert 'id="category"' in html and 'id="from"' in html
    assert all(f'id="{control}"' in html for control in (
        "free-only", "solo-only", "energy", "gender-range",
    ))
    assert "Gratis bestätigt" in html
    assert "Solo-tauglich" in html and "Frauenanteil" in html
    assert "Schätzung" in html and "Stichwörter" in html
    assert 'aria-live="polite"' in html
    assert "prefers-reduced-motion" in html
    assert "innerHTML" not in html
    for category in config.CATEGORIES:
        assert f'"{category}"' in html

    # Complex intent belongs to Wasup's AI surface, not a misleading form.
    assert "Was möchtest du erleben?" not in html
    assert 'type="search"' not in html
    assert 'id="tags"' not in html
    assert "Mehr Filter" not in html
    assert "/v1/search" not in html and "/v1/query" not in html
    assert "Mit KI fragen" in html
    assert html.count('class="nav-link"') == 2
    assert "ai-button" not in html and "subscribe-button" not in html
    assert html.index("<footer>") < html.index('/v1/feed.ics?')
    assert "function browseFilterParams() {\n  const params = new URLSearchParams();" in html
    assert "const params = browseFilterParams();\n  const query = params.size" in html


def test_calendar_gui_remains_keyless_when_api_keys_exist(conn, client):
    conn.execute("INSERT INTO api_key (key, name) VALUES ('calendar-key', 't')")
    conn.commit()
    assert client.get("/calendar").status_code == 200


def test_landing_page_exposes_the_event_browser(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.text.count('href="/calendar"') >= 2
    assert response.text.count('class="nav-link"') == 2
    assert 'href="#use" aria-current="page"' in response.text


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


def test_semantic_sql_membership_remains_cursor_authoritative(
    client, monkeypatch,
):
    """Post-LIMIT diagnostics must never erase an otherwise valid page."""
    from eventindex.api import app as app_mod

    monkeypatch.setattr(
        app_mod.tag_store,
        "semantic_threshold_sql",
        lambda desired, min_match, params, *, prefix,
        min_concept_match=None: ("TRUE", desired),
    )
    monkeypatch.setattr(
        app_mod.tag_store,
        "semantic_matches",
        lambda tx, event_ids, desired: {
            event_id: {
                "score": 0.4999,
                "weakest_concept_score": 0.4999,
                "weakest_concept_query": desired[0],
                "combined_context_score": None,
            }
            for event_id in event_ids
        },
    )

    first = client.get("/v1/occurrences", params={
        "tags": "dance", "min_tag_match": 0.5,
        "radius": "any", "limit": 1,
    }).json()
    assert len(first["occurrences"]) == 1
    assert first["occurrences"][0]["tag_match"] == 0.4999
    assert first["next_cursor"] is not None

    second = client.get("/v1/occurrences", params={
        "tags": "dance", "min_tag_match": 0.5,
        "radius": "any", "limit": 1, "cursor": first["next_cursor"],
    }).json()
    assert len(second["occurrences"]) == 1
    assert second["occurrences"][0]["id"] != first["occurrences"][0]["id"]


def test_event_detail_404(client):
    assert client.get(f"/v1/events/{uuid.uuid4()}").status_code == 404


def test_reads_are_keyless_but_search_and_writes_are_gated(conn, client):
    conn.execute("INSERT INTO api_key (key, name) VALUES ('sekrit', 't')")
    conn.commit()
    # public reads: keyless, even with keys registered
    assert client.get("/v1/occurrences").status_code == 200
    assert client.get("/v1/occurrences/summary").status_code == 200
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


def test_occurrence_summary_is_rate_limited_and_key_bypasses_it(
    conn, client, monkeypatch,
):
    from eventindex.api import app as app_mod

    conn.execute("INSERT INTO api_key (key, name) VALUES ('summary-key', 't')")
    conn.commit()
    monkeypatch.setattr(app_mod, "PUBLIC_READ_RATE_PER_MIN", 2)
    app_mod._rate.clear()
    codes = [client.get("/v1/occurrences/summary").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert client.get(
        "/v1/occurrences/summary", headers={"X-API-Key": "summary-key"},
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
    local_day = (NOW + timedelta(days=1)).astimezone(
        ZoneInfo("Europe/Vienna")
    ).strftime("%A").lower()
    by_weekday = client.get("/v1/feed.ics", params={"weekdays": local_day})
    assert b"Nearby Concert" in by_weekday.content
    assert client.get(
        "/v1/feed.ics", params={"weekdays": "freitag"}
    ).status_code == 422


def test_feed_and_listing_exclude_non_scheduled_occurrences(conn, client):
    event_id = _add_event(
        conn, "Moved Concert", starts=NOW + timedelta(days=4),
        lat=48.3069, lon=14.2858, category=["music"],
    )
    conn.execute(
        "UPDATE occurrence SET status = 'moved' WHERE event_id = %s",
        (event_id,),
    )
    conn.commit()

    listing = client.get(
        "/v1/occurrences", params={"radius": "any", "name": "Moved Concert"}
    )
    feed = client.get(
        "/v1/feed.ics", params={"radius": "any", "name": "Moved Concert"}
    )
    assert _titles(listing) == []
    assert b"Moved Concert" not in feed.content


def test_feed_semantic_tags_filter_before_calendar_membership(
    conn, client, monkeypatch,
):
    from eventindex.api import app as app_mod

    ids = {
        row["title"]: row["id"]
        for row in conn.execute(
            "SELECT id, title FROM event WHERE title IN "
            "('Nearby Concert', 'Unknown Location Talk')"
        )
    }
    def tag_sql(
        desired, min_match, params, *, prefix, min_concept_match=None,
    ):
        assert min_concept_match is None
        params["selected_tag_event"] = ids["Unknown Location Talk"]
        return "e.id = %(selected_tag_event)s", desired

    monkeypatch.setattr(app_mod.tag_store, "semantic_threshold_sql", tag_sql)
    response = client.get(
        "/v1/feed.ics", params={"tags": "workshop", "min_tag_match": 0.5}
    )
    assert b"Unknown Location Talk" in response.content
    assert b"Nearby Concert" not in response.content


def test_rest_tag_concept_floor_requires_tags(client):
    for path in ("/v1/occurrences", "/v1/feed.ics"):
        response = client.get(path, params={"min_tag_concept_match": 0.3})
        assert response.status_code == 422
        assert "requires at least one tag" in response.json()["detail"]


def test_feed_scale_confidence_requires_a_participant_bound(client):
    response = client.get(
        "/v1/feed.ics", params={"min_scale_confidence": 0.3}
    )
    assert response.status_code == 422
    assert "requires participant_count" in response.json()["detail"]


@pytest.mark.parametrize("params", [
    {"near": "91,14", "radius": "5km"},
    {"near": "nan,14", "radius": "5km"},
    {"near": "nan,14", "radius": "any"},
    {"near": "48,14", "radius": "."},
    {"bbox": "15,48,14,49"},
    {"bbox": "14,48,15,inf"},
])
def test_public_geo_inputs_fail_as_422(client, params):
    for path in ("/v1/occurrences", "/v1/feed.ics"):
        assert client.get(path, params=params).status_code == 422


def test_feed_coverage_distinguishes_missing_from_limit(conn, client):
    from eventindex.api.app import _feed_coverage

    event_id = _add_event(
        conn, "Late Music Event", starts=NOW + timedelta(days=10),
        lat=48.3069, lon=14.2858, category=["music"],
    )
    occurrence_id = conn.execute(
        "SELECT id FROM occurrence WHERE event_id = %s", (event_id,)
    ).fetchone()["id"]
    conn.commit()

    coverage = _feed_coverage(
        [uuid.uuid4(), occurrence_id], from_=NOW, radius="any",
        category="music", min_confidence=0,
        include_time_unknown=True, limit=1,
    )
    assert coverage[0]["reasons"] == ["not_found"]
    assert coverage[1]["reasons"] == ["feed_limit"]


def test_feed_all_day_dates_are_local_and_use_date_typed_exclusive_end(conn, client):
    """DATE events must not shift back a day or mix DATE and DATE-TIME.

    The stored UTC value for Vienna midnight belongs to the previous UTC
    date.  RFC 5545 also requires an all-day DTEND to be a DATE and exclusive.
    """
    vienna = ZoneInfo("Europe/Vienna")
    local_day = (datetime.now(vienna) + timedelta(days=10)).date()
    starts = datetime.combine(local_day, time.min, tzinfo=vienna)
    event_id = _add_event(
        conn, "All-day Vienna Festival", starts=starts,
        lat=48.3069, lon=14.2858, category=["art"],
    )
    conn.execute(
        "UPDATE occurrence SET ends_at = %s, time_unknown = true "
        "WHERE event_id = %s",
        (starts + timedelta(days=2), event_id),
    )
    conn.commit()

    resp = client.get("/v1/feed.ics", params={"category": "art"})
    events = list(Calendar.from_ical(resp.content).walk("VEVENT"))
    assert len(events) == 1
    dtstart = events[0].decoded("dtstart")
    dtend = events[0].decoded("dtend")
    assert type(dtstart) is date
    assert type(dtend) is date
    assert dtstart == local_day
    assert dtend == local_day + timedelta(days=3)


def test_feed_can_omit_unknown_time_events_without_changing_public_default(
    conn, client,
):
    event_id = _add_event(
        conn, "Date-only Workshop", starts=NOW + timedelta(days=4),
        lat=48.3069, lon=14.2858, category=["learning"],
    )
    conn.execute(
        "UPDATE occurrence SET time_unknown = true WHERE event_id = %s",
        (event_id,),
    )
    conn.commit()

    default = client.get("/v1/feed.ics", params={"category": "learning"})
    timed_only = client.get(
        "/v1/feed.ics",
        params={"category": "learning", "include_time_unknown": "false"},
    )
    assert b"Date-only Workshop" in default.content
    assert b"Date-only Workshop" not in timed_only.content
    assert b"Unknown Location Talk" in timed_only.content


def test_feed_can_exclude_known_adult_context_without_dropping_unknown(conn, client):
    adult_id = _add_event(
        conn, "Commercial Adult Venue Party", starts=NOW + timedelta(days=1),
        lat=48.3069, lon=14.2858, category=["nightlife"],
    )
    conn.execute(
        "UPDATE event SET inferred = inferred || %s WHERE id = %s",
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


def test_changes_emits_event_once_when_audience_gate_releases(conn, client):
    from eventindex.enrich import (
        apply_audience_essentials, audience_essentials_content_key,
    )

    event_id = _add_event(
        conn, "Wartet auf Audience", starts=NOW + timedelta(days=1),
        category=["culture"],
    )
    conn.execute(
        "UPDATE event SET inferred = '{}'::jsonb, "
        "expected_gender_split = NULL, "
        "expected_gender_split_confidence = NULL, "
        "updated_at = now() - interval '1 day' WHERE id = %s",
        (event_id,),
    )
    conn.execute(
        "UPDATE occurrence SET status = 'pending_enrichment' "
        "WHERE event_id = %s",
        (event_id,),
    )
    conn.commit()

    cursor = None
    while True:
        params = {"limit": 1}
        if cursor is not None:
            params["since"] = cursor
        page = client.get("/v1/changes", params=params).json()
        if not page["events"]:
            break
        cursor = page["next_cursor"]
        assert cursor is not None

    canonical = {
        "title": "Wartet auf Audience", "description": None,
        "category": ["culture"], "venue_name": None,
    }
    attributes = {
        "gender_split": {"value": 0.5, "confidence": 0.2},
        "energy": {"value": "low", "confidence": 0.2},
        "solo_friendly": {"value": True, "confidence": 0.2},
    }
    apply_audience_essentials(
        conn,
        event_id,
        attributes,
        enrichment_key=audience_essentials_content_key(canonical),
    )
    conn.execute(
        "UPDATE occurrence SET status = 'scheduled' WHERE event_id = %s",
        (event_id,),
    )
    conn.commit()

    released = client.get(
        "/v1/changes", params={"since": cursor, "limit": 1},
    ).json()
    assert [row["id"] for row in released["events"]] == [str(event_id)]
    assert released["next_cursor"] is not None
    assert client.get(
        "/v1/changes",
        params={"since": released["next_cursor"], "limit": 1},
    ).json()["events"] == []


def test_query_endpoint_needs_no_llm_and_accepts_partial_filters(client):
    resp = client.post("/v1/query", json={"categories": ["music"]})
    assert resp.status_code == 200
    data = resp.json()
    titles = [o["title"] for o in data["occurrences"]]
    assert "Nearby Concert" in titles
    assert "No Category Thing" not in titles  # null category = unknown (hard)
    assert all("match_score" in o for o in data["occurrences"])


def test_query_endpoint_exposes_semantic_tag_score(conn, client, monkeypatch):
    from eventindex.api import app as app_mod

    ids = {
        row["title"]: row["id"]
        for row in conn.execute(
            "SELECT id, title FROM event WHERE title IN "
            "('Nearby Concert', 'Unknown Location Talk')"
        )
    }
    monkeypatch.setattr(
        app_mod.tag_store,
        "semantic_matches",
        lambda tx, event_ids, desired: {
            ids["Nearby Concert"]: {
                "score": 0.1, "concepts": [],
                "weakest_concept_score": 0.1,
                "weakest_concept_query": "learning",
                "combined_context_score": None,
            },
            ids["Unknown Location Talk"]: {
                "score": 0.8,
                "weakest_concept_score": 0.8,
                "weakest_concept_query": "learning",
                "combined_context_score": None,
                "concepts": [{
                    "query": "learning", "score": 0.8,
                    "event_tag": "learning", "tag_confidence": 0.8,
                    "relatedness": 1.0,
                    "origin": "event_tag", "supports": [],
                    "joint": False, "role": "requested_concept",
                }],
            },
        },
    )
    rows = client.post(
        "/v1/query", json={"tags": ["learning"], "min_tag_match": 0.5}
    ).json()["occurrences"]
    assert [row["title"] for row in rows] == ["Unknown Location Talk"]
    assert rows[0]["tag_match"] == 0.8


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


def test_tentative_events_require_explicit_opt_in(client, conn):
    event_id = _add_event(
        conn, "Tentative Secret Concert",
        starts=NOW + timedelta(days=4),
        lat=48.3069, lon=14.2858,
    )
    conn.execute(
        "UPDATE event SET confidence = 0.39 WHERE id = %s", (event_id,)
    )
    conn.commit()

    assert "Tentative Secret Concert" not in _titles(
        client.get("/v1/occurrences")
    )
    assert "Tentative Secret Concert" in _titles(
        client.get("/v1/occurrences", params={"min_confidence": 0})
    )
    default_query = client.post("/v1/query", json={}).json()["occurrences"]
    assert str(event_id) not in {str(row["event_id"]) for row in default_query}
    tentative_query = client.post(
        "/v1/query", json={"min_confidence": 0},
    ).json()["occurrences"]
    assert str(event_id) in {str(row["event_id"]) for row in tentative_query}


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
    estimates = resp.json()["event"]["estimates"]
    assert estimates["gender_split"] == {"value": 0.5, "confidence": 0.7}
    assert estimates["energy"] == {"value": "medium", "confidence": 0.5}
    assert "_audience_essentials" not in estimates


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
    windows; an event spanning a specifically requested date must match."""
    window_start = (NOW + timedelta(days=20)).replace(microsecond=0)
    window_end = window_start + timedelta(hours=1)
    eid = _add_event(
        conn, "Laufende Ausstellung",
        starts=window_start - timedelta(days=1), category=["art"],
    )
    conn.execute(
        "UPDATE occurrence SET ends_at = %s WHERE event_id = %s",
        (window_start + timedelta(days=1), eid),
    )
    conn.commit()
    body = client.get("/v1/occurrences", params={
        "from": window_start.isoformat(), "to": window_end.isoformat(),
    }).json()
    row = next(o for o in body["occurrences"] if o["title"] == "Laufende Ausstellung")
    assert row["ongoing"] is True
    assert datetime.fromisoformat(row["starts_at"]) == window_start - timedelta(days=1)
    assert datetime.fromisoformat(row["ends_at"]) == window_start + timedelta(days=1)
    body = client.post("/v1/query", json={
        "from_dt": window_start.isoformat(), "to_dt": window_end.isoformat(),
    }).json()
    row = next(o for o in body["occurrences"] if o["title"] == "Laufende Ausstellung")
    assert row["ongoing"] is True


def test_unknown_category_is_422_not_empty(client):
    """B3: a typo'd category silently returned nothing; in
    exclude_categories it silently weakened a guarantee."""
    assert client.post("/v1/query", json={"categories": ["konzert"]}).status_code == 422
    assert client.get("/v1/query", params={"categories": "konzert"}).status_code == 422
    for path in ("/v1/occurrences", "/v1/occurrences/summary", "/v1/feed.ics"):
        assert client.get(path, params={"category": "konzert"}).status_code == 422
        assert client.get(path, params={"category": "music,konzert"}).status_code == 422
        assert client.get(path, params={"category": ""}).status_code == 422
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
    eid = _add_event(
        conn, "Tagesführung", starts=NOW + timedelta(days=1),
        category=["culture"],
    )
    conn.execute("UPDATE event SET kind = 'series' WHERE id = %s", (eid,))
    for d in (2, 3):
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


def test_weekday_filter_selects_the_matching_series_occurrence(client, conn):
    vienna = ZoneInfo("Europe/Vienna")
    local_now = datetime.now(vienna)

    def next_day(isoweekday):
        delta = (isoweekday - local_now.isoweekday()) % 7 or 7
        return (local_now + timedelta(days=delta)).replace(
            hour=20, minute=0, second=0, microsecond=0
        )

    eid = _add_event(
        conn, "Weekday Dance Series", starts=next_day(1),
        category=["nightlife"],
    )
    conn.execute("UPDATE event SET kind = 'series' WHERE id = %s", (eid,))
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s)",
        (eid, next_day(5)),
    )
    conn.commit()

    rows = client.post(
        "/v1/query?distinct=event",
        json={"weekdays": ["thursday", "friday"]},
    ).json()["occurrences"]
    series = next(row for row in rows if row["title"] == "Weekday Dance Series")
    assert datetime.fromisoformat(series["starts_at"]).astimezone(
        vienna
    ).isoweekday() == 5
    assert all(
        datetime.fromisoformat(row["starts_at"]).astimezone(vienna).isoweekday()
        in {4, 5}
        for row in rows
    )


def test_to_dt_bare_date_covers_the_whole_day(client, conn):
    evening = (NOW + timedelta(days=3)).replace(hour=19)
    eid = _add_event(
        conn, "Abendkonzert 18ter", starts=evening, category=["music"],
    )
    conn.commit()
    day = evening.date().isoformat()
    rows = client.post("/v1/query", json={"to_dt": day}).json()["occurrences"]
    assert any(r["title"] == "Abendkonzert 18ter" for r in rows)  # B6


def test_paid_search_returns_explicit_budget_unavailable(client, monkeypatch):
    from eventindex.api import search as search_module
    from eventindex.budget import DailyBudgetExceeded

    monkeypatch.setattr(
        search_module,
        "parse_query",
        lambda *a, **k: (_ for _ in ()).throw(
            DailyBudgetExceeded("daily cap")
        ),
    )
    response = client.get("/v1/search?q=concert")
    assert response.status_code == 503
    assert "POST /v1/query" in response.json()["detail"]
    assert response.headers["retry-after"] == "3600"
