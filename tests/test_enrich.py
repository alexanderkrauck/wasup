"""Enrichment: cache idempotency, confidence cap, typed-column application."""

import uuid

import pytest

from eventindex import enrich as en
from eventindex.enrich import Enrichment, apply_to_event, content_key, enrich_event


def _fake_enrichment(age_conf=0.95):  # over the cap on purpose
    return Enrichment.model_validate({
        "age_min": {"value": 20, "confidence": age_conf, "evidence": "Studentenparty"},
        "age_max": {"value": 30, "confidence": age_conf, "evidence": "Studentenparty"},
        "gender_split": {"value": 0.5, "confidence": 0.3, "evidence": None},
        "expected_attendance": {"value": None, "confidence": 0.0, "evidence": None},
        "language": "de",
        "kid_friendly": {"value": False, "confidence": 0.6, "evidence": "ab 18"},
        "newcomer_friendly": {"value": True, "confidence": 0.5, "evidence": None},
        "outdoor": {"value": None, "confidence": 0.0, "evidence": None},
        "solo_friendly": {"value": True, "confidence": 0.4, "evidence": None},
        "interaction_structure": "optional",
        "energy": "high",
        "sex_service_context": {"value": None, "confidence": 0.0, "evidence": None},
        "vibe_tags": ["techno", "student", "loud"],
        "start_time": {"value": "23:00", "confidence": 0.3, "evidence": None},
    })


@pytest.fixture
def event_row(conn):
    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, category, confidence, status) "
        "VALUES (%s, 'one_off', 'Studentenparty im Keller', '{nightlife}', 0.8, 'confirmed')",
        (event_id,),
    )
    return {
        "id": event_id, "title": "Studentenparty im Keller", "description": None,
        "category": ["nightlife"], "venue_name": "Kellerclub",
        "price_min": None, "price_max": None,
    }


def test_enrich_caches_and_never_pays_twice(conn, event_row, monkeypatch):
    calls = []

    def fake_complete(tx, prompt, schema, **kw):
        calls.append(1)
        return _fake_enrichment()

    monkeypatch.setattr(en.llm, "complete", fake_complete)
    first = enrich_event(conn, event_row)
    second = enrich_event(conn, event_row)
    assert len(calls) == 1  # second hit came from the cache
    assert first == second


def test_confidence_cap_is_code_not_model_discipline(conn, event_row, monkeypatch):
    monkeypatch.setattr(en.llm, "complete", lambda *a, **k: _fake_enrichment(0.99))
    attrs = enrich_event(conn, event_row)
    assert attrs["age_min"]["confidence"] == 0.8


def test_apply_writes_typed_columns_and_inferred(conn, event_row, monkeypatch):
    monkeypatch.setattr(en.llm, "complete", lambda *a, **k: _fake_enrichment())
    attrs = enrich_event(conn, event_row)
    apply_to_event(conn, event_row["id"], attrs)
    row = conn.execute(
        "SELECT expected_age_range, expected_age_range_confidence, inferred "
        "FROM event WHERE id = %s", (event_row["id"],),
    ).fetchone()
    assert row["expected_age_range"].lower == 20
    assert row["expected_age_range"].upper >= 30  # inclusive range storage
    assert row["expected_age_range_confidence"] == 0.8
    assert row["inferred"]["energy"] == "high"
    assert "techno" in row["inferred"]["vibe_tags"]


def test_content_key_changes_with_content(event_row):
    other = dict(event_row, title="Seniorencafé")
    assert content_key(event_row) != content_key(other)


def test_flagged_venue_always_carries_sex_service_context(conn, event_row, monkeypatch):
    """The LLM said unknown, the curated venue flag wins - and it wins on
    the cache-hit path too (flagging a venue must not wait for re-enrichment)."""
    monkeypatch.setattr(en.llm, "complete", lambda *a, **k: _fake_enrichment())
    flagged = dict(event_row, venue_sex_service=True)

    attrs = enrich_event(conn, flagged)
    assert attrs["sex_service_context"] == {
        "value": True, "confidence": 0.8,
        "evidence": "venue is a curated commercial sex establishment",
    }
    # the cache row stays the pure LLM verdict: the override is live, not baked
    cached = conn.execute("SELECT attributes FROM enrichment").fetchone()
    assert cached["attributes"]["sex_service_context"]["value"] is None
    # cache-hit path (same content, e.g. after a rebuild) is overridden too
    assert enrich_event(conn, flagged)["sex_service_context"]["value"] is True
    # an unflagged venue keeps the LLM verdict untouched
    assert enrich_event(conn, event_row)["sex_service_context"]["value"] is None


def test_rebuild_reapply_keeps_venue_override(conn, event_row, monkeypatch):
    """The enrichment cache holds the pure LLM verdict; a rebuild re-applying
    it must not strip the curated venue flag (found live: Football Lounge
    Nights lost the flag on the first rebuild after enrichment)."""
    from eventindex.resolve.rebuild import _apply_enrichment

    venue_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO venue (id, name, sex_service) VALUES (%s, 'Villa Ostende', true)",
        (venue_id,),
    )
    conn.execute(
        "UPDATE event SET venue_id = %s WHERE id = %s", (venue_id, event_row["id"]),
    )
    # seed the cache exactly as enrich_event stores it: LLM said unknown
    monkeypatch.setattr(en.llm, "complete", lambda *a, **k: _fake_enrichment())
    enrich_event(conn, dict(event_row, venue_name="Villa Ostende"))

    pending = _apply_enrichment(conn)
    assert event_row["id"] not in pending  # cache hit, no LLM call needed
    row = conn.execute(
        "SELECT inferred FROM event WHERE id = %s", (event_row["id"],)
    ).fetchone()
    assert row["inferred"]["sex_service_context"]["value"] is True


def test_sex_service_context_lands_in_inferred(conn, event_row, monkeypatch):
    monkeypatch.setattr(en.llm, "complete", lambda *a, **k: _fake_enrichment())
    attrs = enrich_event(conn, dict(event_row, venue_sex_service=True))
    apply_to_event(conn, event_row["id"], attrs)
    row = conn.execute(
        "SELECT inferred FROM event WHERE id = %s", (event_row["id"],)
    ).fetchone()
    assert row["inferred"]["sex_service_context"]["value"] is True
