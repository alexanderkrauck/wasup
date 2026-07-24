import math
import uuid

import numpy as np
import pytest

from eventindex import embeddings, tags


def _event(conn, title: str):
    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, confidence, status) "
        "VALUES (%s, 'one_off', %s, 0.9, 'confirmed')",
        (event_id, title),
    )
    return event_id


def _vector(cosine: float) -> np.ndarray:
    vector = np.zeros(embeddings.DIMENSIONS, dtype=np.float32)
    vector[0] = cosine
    vector[1] = math.sqrt(1 - cosine * cosine)
    return vector


def test_unified_row_merges_origins_and_keeps_highest_certainty(conn):
    event_id = _event(conn, "Salsa Social")
    tags.upsert(conn, event_id, " Dance ", 0.6, "source")
    tags.upsert(conn, event_id, "dance", 0.8, "inferred")
    row = conn.execute(
        "SELECT name, confidence, origins FROM event_tag WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    assert row == {
        "name": "dance", "confidence": 0.8,
        "origins": ["inferred", "source"],
    }


def test_reenrichment_replaces_inferred_certainty_without_erasing_source(conn):
    event_id = _event(conn, "Dance Class")
    tags.upsert(conn, event_id, "dance", 0.4, "source")
    tags.upsert(conn, event_id, "dance", 0.8, "inferred")
    tags.replace_inferred(conn, event_id, [
        {"name": "dance", "confidence": 0.2, "evidence": None}
    ])
    row = conn.execute(
        "SELECT confidence, origins FROM event_tag WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    assert row == {"confidence": 0.4, "origins": ["inferred", "source"]}
    tags.replace_inferred(conn, event_id, [])
    row = conn.execute(
        "SELECT confidence, origins FROM event_tag WHERE event_id = %s",
        (event_id,),
    ).fetchone()
    assert row == {"confidence": 0.4, "origins": ["source"]}


def test_semantic_score_combines_calibrated_relation_and_tag_certainty(
    conn, monkeypatch,
):
    salsa_id = _event(conn, "Salsa Social")
    startup_id = _event(conn, "Startup Meetup")
    exact_id = _event(conn, "Dance Workshop")
    mixed_id = _event(conn, "Low-confidence Exact Plus Strong Relation")
    tags.upsert(conn, salsa_id, "salsa", 0.8, "inferred")
    tags.upsert(conn, startup_id, "startup", 0.8, "inferred")
    tags.upsert(conn, exact_id, "dancing", 0.7, "source")
    tags.upsert(conn, mixed_id, "dancing", 0.3, "inferred")
    tags.upsert(conn, mixed_id, "salsa", 0.8, "inferred")
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO tag_embedding (name, embedding, model) "
            "VALUES (%s, %s::vector, %s)",
            [
                ("salsa", embeddings.vector_literal(_vector(0.65)), embeddings.MODEL_VERSION),
                ("startup", embeddings.vector_literal(_vector(0.10)), embeddings.MODEL_VERSION),
            ],
        )
    query = np.zeros((1, embeddings.DIMENSIONS), dtype=np.float32)
    query[0, 0] = 1
    monkeypatch.setattr(embeddings, "embed_tags", lambda values: query)
    scores = tags.semantic_scores(
        conn, [salsa_id, startup_id, exact_id, mixed_id], ["dancing"]
    )
    assert scores[salsa_id] > 0.7
    assert scores[startup_id] < 0.1
    assert scores[exact_id] == 0.7  # exact tag equality is always relation 1
    assert scores[mixed_id] > 0.7  # weak exact evidence cannot mask stronger support


def test_multiple_desired_tags_measure_joint_concept_coverage(conn, monkeypatch):
    both_id = _event(conn, "Elegant Dance Ball")
    dance_only_id = _event(conn, "Basic Dance Training")
    tags.upsert(conn, both_id, "dance", 0.8, "inferred")
    tags.upsert(conn, both_id, "elegant", 0.7, "inferred")
    tags.upsert(conn, dance_only_id, "dance", 0.9, "inferred")
    # Exact-name branches make embeddings irrelevant while still exercising
    # per-query aggregation.
    monkeypatch.setattr(
        embeddings, "embed_tags",
        lambda values: np.zeros((len(values), embeddings.DIMENSIONS), dtype=np.float32),
    )
    matches = tags.semantic_matches(
        conn, [both_id, dance_only_id], ["dance", "elegant"]
    )
    # Exact evidence keeps its full certainty. Harmonic coverage makes a
    # missing desired concept score zero instead of letting "dance" substitute
    # for "elegant"; absent joint context applies only its bounded 10% penalty.
    harmonic = 2 / (1 / 0.8 + 1 / 0.7)
    assert matches[both_id]["score"] == pytest.approx(0.9 * harmonic)
    assert matches[dance_only_id]["score"] == 0
    assert [m["query"] for m in matches[both_id]["concepts"]] == [
        "dance", "elegant", "dance + elegant"
    ]
    assert matches[both_id]["concepts"][-1]["joint"] is True


def test_joint_context_rejects_embedding_hubs_and_word_sense(conn):
    formal_id = _event(conn, "Graduation Ball")
    sports_id = _event(conn, "Ball Sports Training")
    salsa_id = _event(conn, "Salsa Social")
    sauce_id = _event(conn, "Salsa Cooking")
    event_tags = {
        formal_id: [
            ("formal dance", 0.6), ("maturaball", 0.7),
            ("formal attire", 0.35),
        ],
        sports_id: [
            ("ballsport", 0.8), ("fortbildung", 0.8), ("bewegung", 0.4),
        ],
        salsa_id: [("salsa", 0.8), ("dance", 0.8), ("social dance", 0.6)],
        sauce_id: [("kulinarik", 0.8), ("food", 0.8), ("cooking", 0.6)],
    }
    for event_id, values in event_tags.items():
        for name, confidence in values:
            tags.upsert(conn, event_id, name, confidence, "inferred")
    embeddings.store_missing(
        conn, [name for values in event_tags.values() for name, _ in values]
    )

    ball_scores = tags.semantic_scores(
        conn, [formal_id, sports_id], ["dance", "elegant"]
    )
    salsa_scores = tags.semantic_scores(
        conn, [salsa_id, sauce_id], ["salsa", "dance"]
    )
    reverse_score = tags.semantic_scores(
        conn, [formal_id], ["elegant", "dance"]
    )[formal_id]

    assert ball_scores[formal_id] > ball_scores[sports_id]
    assert salsa_scores[salsa_id] > salsa_scores[sauce_id]
    assert reverse_score == pytest.approx(ball_scores[formal_id])


def test_multi_tag_sql_threshold_uses_the_displayed_rounded_score(conn):
    event_id = _event(conn, "Elegant Dance")
    for name, confidence in [
        ("formal dance", 0.6), ("maturaball", 0.7), ("formal attire", 0.35),
    ]:
        tags.upsert(conn, event_id, name, confidence, "inferred")
    embeddings.store_missing(
        conn, ["formal dance", "maturaball", "formal attire"]
    )
    score = tags.semantic_scores(
        conn, [event_id], ["dance", "elegant"]
    )[event_id]
    params = {}
    condition, _ = tags.semantic_threshold_sql(
        ["dance", "elegant"], round(score, 4), params, prefix="rounded_tag"
    )

    rows = conn.execute(
        f"SELECT e.id FROM event e WHERE {condition}", params
    ).fetchall()

    assert {row["id"] for row in rows} == {event_id}


def test_semantic_threshold_runs_before_sql_limit(conn, monkeypatch):
    salsa_id = _event(conn, "Salsa Social")
    startup_id = _event(conn, "Startup Meetup")
    exact_id = _event(conn, "Dance Class")
    tags.upsert(conn, salsa_id, "salsa", 0.8, "inferred")
    tags.upsert(conn, startup_id, "startup", 0.8, "inferred")
    tags.upsert(conn, exact_id, "dancing", 0.7, "inferred")
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO tag_embedding (name, embedding, model) "
            "VALUES (%s, %s::vector, %s)",
            [
                ("salsa", embeddings.vector_literal(_vector(0.65)), embeddings.MODEL_VERSION),
                ("startup", embeddings.vector_literal(_vector(0.10)), embeddings.MODEL_VERSION),
            ],
        )
    query = np.zeros((1, embeddings.DIMENSIONS), dtype=np.float32)
    query[0, 0] = 1
    monkeypatch.setattr(embeddings, "embed_tags", lambda values: query)
    params = {}
    condition, desired = tags.semantic_threshold_sql(
        ["dancing"], 0.5, params, prefix="test_tag"
    )
    rows = conn.execute(
        f"SELECT e.id FROM event e WHERE {condition} ORDER BY e.id",
        params,
    ).fetchall()
    assert desired == ["dancing"]
    assert {row["id"] for row in rows} == {salsa_id, exact_id}


def test_tag_sanity_rejects_commentary_and_merges_duplicates():
    cleaned = tags.clean_estimates([
        {"name": " Partner   Dancing ", "confidence": 0.6, "evidence": None},
        {"name": "partner dancing", "confidence": 0.8, "evidence": "Salsa"},
        {"name": "dance (probably)", "confidence": 0.9, "evidence": None},
        {"name": "far too many words for one event tag", "confidence": 0.9,
         "evidence": None},
    ])
    assert cleaned == [
        {"name": "partner dancing", "confidence": 0.8, "evidence": "Salsa"}
    ]


def test_public_tag_queries_are_bounded_and_validated():
    assert tags.clean_desired([" Dancing ", "dancing", "latin dance"]) == [
        "dancing", "latin dance"
    ]
    try:
        tags.clean_desired(["far too many words for one tag"])
    except ValueError as exc:
        assert "1-3 words" in str(exc)
    else:
        raise AssertionError("invalid tag query was accepted")
