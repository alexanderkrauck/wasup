"""Enrichment: cache idempotency, confidence cap, typed-column application."""

import uuid
from threading import Barrier, Thread

import psycopg
import pytest
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from eventindex import enrich as en
from eventindex.enrich import (
    AudienceEssentialsBatch,
    Enrichment,
    apply_audience_essentials,
    apply_to_event,
    audience_essentials_content_key,
    content_key,
    enrich_event,
    estimate_audience_essentials,
)


def _fake_enrichment(age_conf=0.95):  # over the cap on purpose
    return Enrichment.model_validate({
        "age_min": {"value": 20, "confidence": age_conf, "evidence": "Studentenparty"},
        "age_max": {"value": 30, "confidence": age_conf, "evidence": "Studentenparty"},
        "gender_split": {"value": 0.5, "confidence": 0.3, "evidence": None},
        "event_scale": {
            "estimated_participants": 250,
            "plausible_min": 150,
            "plausible_max": 400,
            "confidence": 0.35,
            "basis": ["club capacity", "event format"],
            "evidence": None,
        },
        "language": {"value": "de", "confidence": 0.7, "evidence": "Studentenparty"},
        "kid_friendly": {"value": False, "confidence": 0.6, "evidence": "ab 18"},
        "newcomer_friendly": {"value": True, "confidence": 0.5, "evidence": None},
        "outdoor": {"value": False, "confidence": 0.2, "evidence": None},
        "solo_friendly": {"value": True, "confidence": 0.4, "evidence": None},
        "interaction_structure": "optional",
        "energy": "high",
        "sex_service_context": {"value": False, "confidence": 0.2, "evidence": None},
        "tags": [
            {"name": "techno", "confidence": 0.9, "evidence": "Studentenparty"},
            {"name": "student nightlife", "confidence": 0.6, "evidence": "Studentenparty"},
            {"name": "loud", "confidence": 0.3, "evidence": None},
            {"name": "dance", "confidence": 0.3, "evidence": None},
            {"name": "night out", "confidence": 0.3, "evidence": None},
            {"name": "club music", "confidence": 0.3, "evidence": None},
        ],
        "venue": {"value": "Kellerclub", "confidence": 0.8, "evidence": "im Kellerclub"},
        "price": {
            "min": 12, "max": 12, "currency": "EUR", "confidence": 0.8,
            "basis": "stated", "evidence": "12 EUR",
        },
        "start_time": {"value": "23:00", "confidence": 0.3, "evidence": None},
    })


def _fake_audience_batch(event_ids):
    return AudienceEssentialsBatch.model_validate({
        "events": [
            {
                "event_id": str(event_id),
                "gender_split": {
                    "value": 0.62,
                    "confidence": 0.9,
                },
                "energy": {
                    "value": "high",
                    "confidence": 0.7,
                },
                "solo_friendly": {
                    "value": True,
                    "confidence": 0.6,
                },
            }
            for event_id in event_ids
        ]
    })


@pytest.fixture
def event_row(conn):
    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, description, category, "
        "confidence, status) VALUES (%s, 'one_off', "
        "'Studentenparty im Keller', 'Eintritt 12 EUR im Kellerclub', '{nightlife}', "
        "0.8, 'confirmed')",
        (event_id,),
    )
    return {
        "id": event_id, "title": "Studentenparty im Keller",
        "description": "Eintritt 12 EUR im Kellerclub",
        "category": ["nightlife"], "venue_name": "Kellerclub",
        "price_min": None, "price_max": None,
    }


def test_enrich_caches_and_never_pays_twice(conn, event_row, monkeypatch):
    calls = []

    def fake_complete(tx, prompt, schema, **kw):
        calls.append(prompt)
        return _fake_enrichment()

    monkeypatch.setattr(en.llm, "complete", fake_complete)
    first = enrich_event(conn, event_row)
    second = enrich_event(conn, event_row)
    assert len(calls) == 1  # second hit came from the cache
    assert "core named activity or event format" in calls[0]
    assert "atmosphere/style" in calls[0]
    assert "what participants actually do" in calls[0]
    assert "meaningful secondary activities" in calls[0]
    assert "already represented by structured fields" in calls[0]
    assert "Never add one-word head tags mechanically" in calls[0]
    assert "exact original source-language quote" in calls[0]
    assert first == second


def test_audience_essentials_are_batched_cached_and_cheap(
    conn, event_row, monkeypatch,
):
    second_id = uuid.uuid4()
    second = dict(
        event_row,
        id=second_id,
        title="Leiser Leseabend",
        description="Gemeinsames stilles Lesen",
        category=["learning"],
    )
    calls = []

    def fake_complete(tx, prompt, schema, **kwargs):
        calls.append((prompt, schema, kwargs))
        return _fake_audience_batch([event_row["id"], second_id])

    monkeypatch.setattr(en.llm, "complete", fake_complete)
    first = estimate_audience_essentials(conn, [event_row, second], job_id=uuid.uuid4())
    again = estimate_audience_essentials(conn, [event_row, second], job_id=uuid.uuid4())

    assert first == again
    assert len(calls) == 1
    prompt, schema, kwargs = calls[0]
    assert schema is AudienceEssentialsBatch
    assert "no value may be null or unknown" in prompt
    assert kwargs["budget_lane"] == "core"
    assert kwargs["max_tokens"] == en.config.AUDIENCE_ESSENTIALS_MAX_OUTPUT_TOKENS
    assert kwargs["reservation_eur"] == \
        en.config.AUDIENCE_ESSENTIALS_RESERVATION_EUR
    assert kwargs["reasoning_effort"] == "none"
    assert audience_essentials_content_key(event_row) != content_key(event_row)
    assert first[str(event_row["id"])][1] == {
        "gender_split": {
            "value": 0.62,
            "confidence": 0.35,
        },
        "energy": {
            "value": "high",
            "confidence": 0.35,
        },
        "solo_friendly": {
            "value": True,
            "confidence": 0.35,
        },
    }
    assert conn.execute(
        "SELECT count(*) AS n FROM enrichment"
    ).fetchone()["n"] == 2


def test_full_enrichment_cannot_overwrite_committed_audience_partition(
    conn, event_row,
):
    compact = {
        "gender_split": {"value": 0.2, "confidence": 0.25},
        "energy": {"value": "low", "confidence": 0.25},
        "solo_friendly": {
            "value": False, "confidence": 0.25,
        },
    }
    full = {
        "gender_split": {"value": 0.8, "confidence": 0.5, "evidence": None},
        "energy": "high",
        "solo_friendly": {
            "value": True, "confidence": 0.5, "evidence": None,
        },
    }
    _, committed = en.persist_audience_essentials(
        conn, event_row, compact, model="compact"
    )
    _, winner = en.persist_audience_essentials(
        conn,
        event_row,
        en.audience_essentials_from_full(full),
        model="full",
    )

    assert winner == committed
    merged = en.with_audience_essentials(full, winner)
    assert merged["gender_split"] == compact["gender_split"]
    assert merged["energy"] == "low"
    assert merged["solo_friendly"] == compact["solo_friendly"]


@pytest.mark.parametrize("invalid", [None, [], "broken", 42])
def test_invalid_full_cache_falls_back_to_compact_repair(invalid):
    assert en.audience_essentials_from_full(invalid) is None


def test_apply_audience_essentials_writes_every_publication_gate(
    conn, event_row,
):
    conn.execute(
        "UPDATE event SET updated_at = now() - interval '1 day' WHERE id = %s",
        (event_row["id"],),
    )
    before = conn.execute(
        "SELECT updated_at FROM event WHERE id = %s", (event_row["id"],),
    ).fetchone()["updated_at"]
    attrs = _fake_audience_batch([event_row["id"]]).events[0].model_dump(
        exclude={"event_id"}
    )
    key = audience_essentials_content_key(event_row)

    apply_audience_essentials(
        conn, event_row["id"], attrs, enrichment_key=key
    )

    row = conn.execute(
        "SELECT expected_gender_split, expected_gender_split_confidence, "
        "inferred, updated_at FROM event WHERE id = %s",
        (event_row["id"],),
    ).fetchone()
    assert row["expected_gender_split"] == 0.62
    assert row["expected_gender_split_confidence"] == 0.8
    assert row["updated_at"] > before
    assert row["inferred"]["energy"] == "high"
    assert row["inferred"]["solo_friendly"]["value"] is True
    meta = row["inferred"]["_audience_essentials"]
    assert meta["content_key"] == key
    assert meta["energy"]["confidence"] == 0.7
    assert meta["solo_friendly"]["confidence"] == 0.6

    apply_audience_essentials(
        conn, event_row["id"], attrs, enrichment_key=key
    )
    assert conn.execute(
        "SELECT updated_at FROM event WHERE id = %s", (event_row["id"],),
    ).fetchone()["updated_at"] == row["updated_at"]


def test_audience_readiness_timestamp_never_moves_backwards(
    conn, event_row,
):
    future = conn.execute(
        "UPDATE event SET updated_at = statement_timestamp() + interval '1 hour' "
        "WHERE id = %s RETURNING updated_at",
        (event_row["id"],),
    ).fetchone()["updated_at"]
    attrs = _fake_audience_batch([event_row["id"]]).events[0].model_dump(
        exclude={"event_id"}
    )

    apply_audience_essentials(
        conn,
        event_row["id"],
        attrs,
        enrichment_key=audience_essentials_content_key(event_row),
    )

    row = conn.execute(
        "SELECT inferred, updated_at FROM event WHERE id = %s",
        (event_row["id"],),
    ).fetchone()
    assert row["inferred"]["energy"] == "high"
    assert row["updated_at"] > future


def test_concurrent_cache_miss_returns_the_committed_winner(
    conn, test_db_url, event_row, monkeypatch,
):
    """Both workers may pay, but both must apply the one persisted verdict."""
    barrier = Barrier(2)

    def fake_complete(*args, **kwargs):
        raw = _fake_enrichment().model_dump()
        estimate = 250 if __import__("threading").current_thread().name == "a" else 600
        raw["event_scale"].update({
            "estimated_participants": estimate,
            "plausible_min": 100,
            "plausible_max": 800,
        })
        barrier.wait(timeout=5)
        return Enrichment.model_validate(raw)

    monkeypatch.setattr(en.llm, "complete", fake_complete)
    results = []

    def run():
        with psycopg.connect(test_db_url, row_factory=dict_row) as worker_conn:
            results.append(enrich_event(worker_conn, event_row))

    threads = [Thread(target=run, name=name) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    persisted = conn.execute(
        "SELECT attributes FROM enrichment WHERE content_key = %s",
        (content_key(event_row),),
    ).fetchone()["attributes"]
    expected = persisted["event_scale"]["estimated_participants"]
    assert len(results) == 2
    assert {
        result["event_scale"]["estimated_participants"] for result in results
    } == {expected}


def test_confidence_cap_is_code_not_model_discipline(conn, event_row, monkeypatch):
    monkeypatch.setattr(en.llm, "complete", lambda *a, **k: _fake_enrichment(0.99))
    attrs = enrich_event(conn, event_row)
    assert attrs["age_min"]["confidence"] == 0.8
    assert attrs["newcomer_friendly"]["confidence"] == 0.35
    assert attrs["solo_friendly"]["confidence"] == 0.35


def test_unsupported_tag_certainty_is_capped_at_prior_tier(
    conn, event_row, monkeypatch,
):
    raw = _fake_enrichment().model_dump()
    raw["tags"][3]["confidence"] = 0.8
    raw["tags"][3]["evidence"] = None
    monkeypatch.setattr(
        en.llm, "complete", lambda *a, **k: Enrichment.model_validate(raw)
    )

    attrs = enrich_event(conn, event_row)

    dance = next(tag for tag in attrs["tags"] if tag["name"] == "dance")
    assert dance["confidence"] == 0.35


def test_enrichment_schema_requires_six_tags_after_filler_cleanup():
    raw = _fake_enrichment().model_dump()
    raw["tags"][-1] = {
        "name": "evening event", "confidence": 0.35, "evidence": None,
    }

    with pytest.raises(
        ValueError,
        match="at least 6 distinct useful concepts after.*filler is removed",
    ):
        Enrichment.model_validate(raw)


def test_normalized_tag_keeps_high_confidence_with_german_evidence(
    conn, event_row, monkeypatch,
):
    raw = _fake_enrichment().model_dump()
    raw["tags"][3] = {
        "name": "movement",
        "confidence": 0.8,
        "evidence": "Bewegen zu den Klangwelten",
    }
    event = dict(
        event_row,
        description=(
            "Der Raum ist offen für Lauschen und Bewegen zu den Klangwelten."
        ),
    )
    monkeypatch.setattr(
        en.llm, "complete", lambda *a, **k: Enrichment.model_validate(raw)
    )

    attrs = enrich_event(conn, event)

    movement = next(tag for tag in attrs["tags"] if tag["name"] == "movement")
    assert movement == {
        "name": "movement",
        "confidence": 0.8,
        "evidence": "Bewegen zu den Klangwelten",
    }


def test_explanatory_non_quote_does_not_unlock_high_certainty(
    conn, event_row, monkeypatch,
):
    raw = _fake_enrichment().model_dump()
    raw["sex_service_context"] = {
        "value": False,
        "confidence": 0.8,
        "evidence": "no indication of commercial sex services",
    }
    monkeypatch.setattr(
        en.llm, "complete", lambda *a, **k: Enrichment.model_validate(raw)
    )

    attrs = enrich_event(conn, event_row)

    assert attrs["sex_service_context"] == {
        "value": False,
        "confidence": 0.35,
        "evidence": None,
    }


def test_apply_writes_typed_columns_and_inferred(conn, event_row, monkeypatch):
    monkeypatch.setattr(en.llm, "complete", lambda *a, **k: _fake_enrichment())
    attrs = enrich_event(conn, event_row)
    apply_to_event(conn, event_row["id"], attrs)
    row = conn.execute(
        "SELECT expected_age_range, expected_age_range_confidence, inferred, "
        "lang, price_min, price_max, venue_id, expected_attendance, "
        "expected_attendance_confidence "
        "FROM event WHERE id = %s", (event_row["id"],),
    ).fetchone()
    assert row["expected_age_range"].lower == 20
    assert row["expected_age_range"].upper >= 30  # inclusive range storage
    assert row["expected_age_range_confidence"] == 0.8
    assert row["inferred"]["energy"] == "high"
    assert row["inferred"]["language"]["value"] == "de"
    assert row["lang"] == "de"
    assert float(row["price_min"]) == float(row["price_max"]) == 12
    assert row["expected_attendance"] == 250
    assert row["expected_attendance_confidence"] == 0.35
    assert row["venue_id"] is not None
    tags = conn.execute(
        "SELECT name, confidence, origins FROM event_tag WHERE event_id = %s",
        (event_row["id"],),
    ).fetchall()
    assert {tag["name"] for tag in tags} == {
        "techno", "student nightlife", "loud", "dance", "night out",
        "club music",
    }
    assert next(tag for tag in tags if tag["name"] == "techno")["confidence"] == 0.8

    updated_at = conn.execute(
        "SELECT updated_at FROM event WHERE id = %s", (event_row["id"],),
    ).fetchone()["updated_at"]
    apply_to_event(conn, event_row["id"], attrs)
    assert conn.execute(
        "SELECT updated_at FROM event WHERE id = %s", (event_row["id"],),
    ).fetchone()["updated_at"] == updated_at


def test_content_key_changes_with_content(event_row):
    other = dict(event_row, title="Seniorencafé")
    assert content_key(event_row) != content_key(other)
    assert content_key(dict(event_row, price_min=0, price_max=0)) != \
        content_key(event_row)


def test_flagged_venue_always_carries_sex_service_context(conn, event_row, monkeypatch):
    """The LLM's low-certainty false estimate is overridden by curated facts,
    and the override wins on
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
    assert cached["attributes"]["sex_service_context"]["value"] is False
    # cache-hit path (same content, e.g. after a rebuild) is overridden too
    assert enrich_event(conn, flagged)["sex_service_context"]["value"] is True
    # an unflagged venue keeps the LLM verdict untouched
    assert enrich_event(conn, event_row)["sex_service_context"]["value"] is False


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


def test_rebuild_gates_unknown_audience_and_releases_cached_essentials(
    conn, event_row,
):
    from eventindex.resolve.rebuild import _apply_enrichment

    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at, status) "
        "VALUES (%s, now() + interval '1 day', 'scheduled')",
        (event_row["id"],),
    )

    pending = _apply_enrichment(conn)

    assert pending == [event_row["id"]]
    assert conn.execute(
        "SELECT status FROM occurrence WHERE event_id = %s",
        (event_row["id"],),
    ).fetchone()["status"] == "pending_enrichment"

    attrs = _fake_audience_batch([event_row["id"]]).events[0].model_dump(
        exclude={"event_id"}
    )
    # The canonical row has no venue_id, so rebuild sees no venue name.
    key = audience_essentials_content_key(dict(event_row, venue_name=None))
    conn.execute(
        "INSERT INTO enrichment (content_key, attributes) VALUES (%s, %s)",
        (key, Jsonb(attrs)),
    )
    conn.execute(
        "UPDATE occurrence SET status = 'scheduled' WHERE event_id = %s",
        (event_row["id"],),
    )

    assert _apply_enrichment(conn) == [event_row["id"]]
    row = conn.execute(
        "SELECT expected_gender_split, inferred FROM event WHERE id = %s",
        (event_row["id"],),
    ).fetchone()
    assert row["expected_gender_split"] == 0.62
    assert row["inferred"]["energy"] == "high"
    assert row["inferred"]["solo_friendly"]["value"] is True
    assert conn.execute(
        "SELECT status FROM occurrence WHERE event_id = %s",
        (event_row["id"],),
    ).fetchone()["status"] == "scheduled"


def test_sex_service_context_lands_in_inferred(conn, event_row, monkeypatch):
    monkeypatch.setattr(en.llm, "complete", lambda *a, **k: _fake_enrichment())
    attrs = enrich_event(conn, dict(event_row, venue_sex_service=True))
    apply_to_event(conn, event_row["id"], attrs)
    row = conn.execute(
        "SELECT inferred FROM event WHERE id = %s", (event_row["id"],)
    ).fetchone()
    assert row["inferred"]["sex_service_context"]["value"] is True
