import uuid
from datetime import timedelta

from psycopg.types.json import Jsonb

from eventindex import config
from eventindex.enrich.facts import PublicPage
from eventindex.enrich import venue_facts
from eventindex.enrich.venue_facts import VenueCapacity


def _future_venue(conn):
    venue_id = conn.execute(
        "INSERT INTO venue (name) VALUES ('Design Center Linz') RETURNING id"
    ).fetchone()["id"]
    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, venue_id, confidence, status) "
        "VALUES (%s, 'one_off', 'Messe', %s, 0.8, 'confirmed')",
        (event_id, venue_id),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, now() + %s)",
        (event_id, timedelta(days=30)),
    )
    return venue_id


def test_capacity_requires_verbatim_public_evidence(conn, monkeypatch):
    pages = [PublicPage(
        "https://venue.example/raeume",
        "Der Große Saal bietet Platz für maximal 3.000 Personen.",
    )]
    monkeypatch.setattr(
        venue_facts.llm,
        "complete",
        lambda *a, **k: VenueCapacity(
            same_venue=True,
            capacity=3000,
            evidence="Platz für maximal 3.000 Personen",
            source=0,
            confidence=0.8,
        ),
    )
    capacity, evidence, url = venue_facts.extract_capacity(
        conn, {"name": "Design Center Linz", "address": None}, pages
    )
    assert capacity == 3000
    assert "3.000" in evidence
    assert url == pages[0].url


def test_ambiguous_repeated_place_name_stays_unknown(monkeypatch):
    request = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"places": [
                {
                    "id": "one",
                    "displayName": {"text": "Pfarrsaal"},
                    "formattedAddress": "First 1",
                    "location": {"latitude": 48.30, "longitude": 14.28},
                },
                {
                    "id": "two",
                    "displayName": {"text": "Pfarrsaal"},
                    "formattedAddress": "Second 2",
                    "location": {"latitude": 48.31, "longitude": 14.29},
                },
            ]}

    monkeypatch.setattr(venue_facts.config, "GOOGLE_PLACES_API_KEY", "test")
    def post(*args, **kwargs):
        request.update(kwargs)
        return Response()

    monkeypatch.setattr(venue_facts.httpx, "post", post)
    monkeypatch.setattr(venue_facts, "record_spend", lambda *a, **k: None)

    assert venue_facts.find_place(
        {"name": "Pfarrsaal", "address": None}
    ) is None
    assert (
        request["json"]["locationBias"]["circle"]["radius"]
        <= 50_000
    )


def test_postcode_locality_is_not_treated_as_a_venue(monkeypatch):
    monkeypatch.setattr(
        venue_facts.httpx,
        "post",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("a locality placeholder must not reach Places")
        ),
    )

    assert venue_facts.is_location_only("4020, Linz")
    assert venue_facts.is_location_only("3430 Tulln an der Donau")
    assert not venue_facts.is_location_only("Design Center Linz")
    assert venue_facts.find_place(
        {"name": "4020, Linz", "address": None}
    ) is None


def test_ground_venue_populates_place_fields_and_capacity(conn, monkeypatch):
    from eventindex.discovery import sweep
    from eventindex.jobs import handlers

    venue_id = _future_venue(conn)
    place = {
        "id": "places/design-center",
        "formattedAddress": "Europaplatz 1, 4020 Linz",
        "location": {"latitude": 48.298, "longitude": 14.302},
        "websiteUri": "https://venue.example",
    }
    monkeypatch.setattr(
        venue_facts, "find_place", lambda *a, **k: place
    )
    monkeypatch.setattr(
        sweep, "search_web", lambda *a, **k: ["https://venue.example/capacity"]
    )
    monkeypatch.setattr(
        __import__(
            "eventindex.enrich.facts", fromlist=["fetch_pages"]
        ),
        "fetch_pages",
        lambda urls: [PublicPage(urls[0], "Kapazität 3000")],
    )
    monkeypatch.setattr(
        venue_facts, "extract_capacity",
        lambda *a, **k: (3000, "Kapazität 3000", "https://venue.example"),
    )

    assert handlers.ground_venue(
        {"id": uuid.uuid4(), "payload": {"venue_id": str(venue_id)}}, conn
    ) == []
    row = conn.execute(
        "SELECT address, capacity, gmaps_place_id, "
        "ST_Y(geo) AS lat, ST_X(geo) AS lon FROM venue WHERE id = %s",
        (venue_id,),
    ).fetchone()
    assert row["address"] == "Europaplatz 1, 4020 Linz"
    assert row["capacity"] == 3000
    assert row["gmaps_place_id"] == "places/design-center"
    assert round(row["lat"], 3) == 48.298


def test_capacity_search_requires_a_corroborated_place(conn, monkeypatch):
    from eventindex.discovery import sweep
    from eventindex.jobs import handlers

    venue_id = _future_venue(conn)
    monkeypatch.setattr(venue_facts, "find_place", lambda *a, **k: None)
    monkeypatch.setattr(
        sweep,
        "search_web",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("name-only capacity search is ambiguous")
        ),
    )

    handlers.ground_venue(
        {"id": uuid.uuid4(), "payload": {"venue_id": str(venue_id)}}, conn
    )
    row = conn.execute(
        "SELECT capacity, gmaps_place_id FROM venue WHERE id = %s",
        (venue_id,),
    ).fetchone()
    assert row["capacity"] is None
    assert row["gmaps_place_id"] is None


def test_unsafe_rollout_capacity_migration_uses_its_provenance(conn):
    venue_id = conn.execute(
        "INSERT INTO venue (name, capacity, gmaps_place_id) "
        "VALUES ('Backstube', 100, 'keep-place') RETURNING id"
    ).fetchone()["id"]
    job_id = conn.execute(
        "INSERT INTO jobs (kind, payload, status) "
        "VALUES ('ground_venue', %s, 'done') RETURNING id",
        (Jsonb({"venue_id": str(venue_id)}),),
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO crawl_log (job_id, status, detail) VALUES "
        "(%s, 'ok', 'ground_venue: matched=False capacity=100')",
        (job_id,),
    )
    conn.execute(
        (config.MIGRATIONS_DIR / "016_unsafe_venue_capacity.sql").read_text()
    )

    row = conn.execute(
        "SELECT capacity, gmaps_place_id FROM venue WHERE id = %s",
        (venue_id,),
    ).fetchone()
    assert row["capacity"] is None
    # The prior Place match remains for a named venue; only its unsupported
    # capacity is removed.
    assert row["gmaps_place_id"] == "keep-place"


def test_scheduler_bounds_and_deduplicates_venue_grounding(conn):
    from eventindex.jobs.schedule import enqueue_venue_grounding

    venue_id = _future_venue(conn)
    assert enqueue_venue_grounding(conn) == 1
    job = conn.execute(
        "SELECT payload FROM jobs WHERE kind = 'ground_venue'"
    ).fetchone()
    assert job["payload"]["venue_id"] == str(venue_id)
    assert enqueue_venue_grounding(conn) == 0
