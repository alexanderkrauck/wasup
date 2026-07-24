import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from psycopg.types.json import Jsonb

from eventindex.enrich import facts
from eventindex.enrich.facts import PublicPage, RecoveredFacts, extract_facts


def _event():
    return {
        "title": "Maturaball 2026",
        "starts_at": datetime(2026, 10, 17, tzinfo=ZoneInfo("Europe/Vienna")),
        "venue_name": "Palais Kaufmännischer Verein",
        "organizer": "HLW",
        "source_id": None,
    }


def test_recovered_facts_require_verbatim_public_evidence(conn, monkeypatch):
    pages = [PublicPage(
        "https://tickets.example/maturaball",
        "Maturaball 2026 am 17. Oktober. Einlass 19 Uhr, Beginn 20:00. "
        "Karten kosten 28 EUR. Palais Kaufmännischer Verein. "
        "https://tickets.example/maturaball",
    )]
    answer = RecoveredFacts(
        same_event=True,
        price_min=28,
        price_max=28,
        price_evidence="Karten kosten 28 EUR",
        price_source=0,
        venue_name="Palais Kaufmännischer Verein",
        venue_evidence="Palais Kaufmännischer Verein",
        venue_source=0,
        booking_url="https://tickets.example/maturaball",
        booking_evidence="https://tickets.example/maturaball",
        booking_source=0,
        start_time="20:00",
        start_time_evidence="Beginn 20:00",
        start_time_source=0,
        confidence=0.95,
    )
    monkeypatch.setattr(facts.llm, "complete", lambda *a, **k: answer)

    payload, raw = extract_facts(conn, _event(), pages)
    assert payload["price_min"] == {"value": 28, "confidence": 0.85}
    assert payload["venue_name"]["value"] == \
        "Palais Kaufmännischer Verein"
    assert payload["booking_url"]["value"] == pages[0].url
    assert payload["starts_at"]["value"].startswith("2026-10-17T20:00:00")
    assert "28 EUR" in raw


def test_unsupported_claimed_facts_are_not_emitted(conn, monkeypatch):
    pages = [PublicPage(
        "https://example.test/event",
        "Maturaball 2026 am 17. Oktober. Weitere Details folgen.",
    )]
    answer = RecoveredFacts(
        same_event=True,
        price_min=28,
        price_max=28,
        price_evidence="Karten kosten 28 EUR",
        price_source=0,
        venue_name=None,
        venue_evidence=None,
        venue_source=None,
        booking_url=None,
        booking_evidence=None,
        booking_source=None,
        start_time=None,
        start_time_evidence=None,
        start_time_source=None,
        confidence=0.8,
    )
    monkeypatch.setattr(facts.llm, "complete", lambda *a, **k: answer)

    payload, raw = extract_facts(conn, _event(), pages)
    assert "price_min" not in payload
    assert raw is None


def _seed_hydratable_event(conn):
    source_id = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, status) "
        "VALUES ('Generic Source', 'https://events.example', 'website', 2, "
        "0.7, 'active') RETURNING id"
    ).fetchone()["id"]
    venue_id = conn.execute(
        "INSERT INTO venue (name) VALUES ('Ballroom') RETURNING id"
    ).fetchone()["id"]
    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, category, confidence, status, "
        "url, booking_url, venue_id) VALUES (%s, 'one_off', 'Example Gala', "
        "'{culture}', 0.8, 'confirmed', 'https://events.example/gala', "
        "'https://tickets.example/gala', %s)",
        (event_id, venue_id),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) "
        "VALUES (%s, now() + interval '30 days')",
        (event_id,),
    )
    fingerprint = f"hydrate-{event_id}"
    conn.execute(
        "INSERT INTO event_claim (source_id, fingerprint, payload) "
        "VALUES (%s, %s, %s)",
        (source_id, fingerprint, Jsonb({
            "title": {"value": "Example Gala", "confidence": 0.8},
        })),
    )
    conn.execute(
        "INSERT INTO identity (fingerprint, event_id) VALUES (%s, %s)",
        (fingerprint, event_id),
    )
    return event_id


def test_hydration_appends_a_claim_and_enqueues_resolve(conn, monkeypatch):
    from eventindex.discovery import sweep
    from eventindex.jobs.handlers import hydrate_event

    event_id = _seed_hydratable_event(conn)
    page = PublicPage("https://tickets.example/gala", "Tickets 28 EUR")
    monkeypatch.setattr(facts, "fetch_pages", lambda urls: [page])
    monkeypatch.setattr(
        facts, "extract_facts",
        lambda *args, **kwargs: ({
            "price_min": {"value": 28, "confidence": 0.85},
            "price_max": {"value": 28, "confidence": 0.85},
            "url": {"value": page.url, "confidence": 0.85},
        }, "28 EUR"),
    )
    monkeypatch.setattr(
        sweep, "search_web",
        lambda *args, **kwargs: pytest.fail(
            "complete detail must not web-search"
        ),
    )

    jobs = hydrate_event(
        {"id": uuid.uuid4(), "payload": {"event_id": str(event_id)}}, conn
    )
    claim = conn.execute(
        "SELECT payload, raw_excerpt FROM event_claim "
        "WHERE fingerprint = %s AND payload ? 'price_min' LIMIT 1",
        (f"hydrate-{event_id}",),
    ).fetchone()
    assert claim["payload"]["price_min"]["value"] == 28
    assert claim["payload"]["url"]["value"] == page.url
    assert claim["raw_excerpt"] == "28 EUR"
    assert jobs == [{"kind": "resolve", "payload": {}}]


def test_web_recovery_price_keeps_the_actual_evidence_url(conn, monkeypatch):
    from eventindex.discovery import sweep
    from eventindex.jobs.handlers import hydrate_event

    event_id = _seed_hydratable_event(conn)
    event_page = PublicPage(
        "https://events.example/gala", "Example Gala in the Ballroom"
    )
    price_page = PublicPage(
        "https://tickets.example/gala-price", "Eintritt: 39 EUR"
    )
    fetch_calls = []

    def fake_fetch(urls):
        fetch_calls.append(urls)
        return [event_page] if len(fetch_calls) == 1 else [price_page]

    extract_calls = []

    def fake_extract(tx, row, pages, **kwargs):
        extract_calls.append(pages)
        if len(extract_calls) == 1:
            return {}, None
        return {
            "price_min": {"value": 39, "confidence": 0.85},
            "price_max": {"value": 39, "confidence": 0.85},
            "url": {"value": price_page.url, "confidence": 0.85},
        }, "Eintritt: 39 EUR"

    monkeypatch.setattr(facts, "fetch_pages", fake_fetch)
    monkeypatch.setattr(facts, "extract_facts", fake_extract)
    monkeypatch.setattr(sweep, "search_web", lambda *a, **k: [price_page.url])

    hydrate_event(
        {"id": uuid.uuid4(), "payload": {"event_id": str(event_id)}}, conn
    )
    claim = conn.execute(
        "SELECT payload FROM event_claim WHERE fingerprint = %s "
        "AND payload ? 'price_min' LIMIT 1",
        (f"hydrate-{event_id}",),
    ).fetchone()
    assert claim["payload"]["price_min"]["value"] == 39
    assert claim["payload"]["url"]["value"] == price_page.url
