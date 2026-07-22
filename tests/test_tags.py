import math
import uuid

import numpy as np

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
    tags.upsert(conn, salsa_id, "salsa", 0.8, "inferred")
    tags.upsert(conn, startup_id, "startup", 0.8, "inferred")
    tags.upsert(conn, exact_id, "dancing", 0.7, "source")
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
        conn, [salsa_id, startup_id, exact_id], ["dancing"]
    )
    assert scores[salsa_id] > 0.7
    assert scores[startup_id] < 0.1
    assert scores[exact_id] == 0.7  # exact tag equality is always relation 1


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
