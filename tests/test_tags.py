import math
import uuid

import numpy as np
import pytest
from psycopg.types.json import Jsonb

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


def test_focused_relatedness_beats_a_broader_higher_confidence_neighbour(
    conn, monkeypatch,
):
    close_id = _event(conn, "Direct Topic")
    broad_id = _event(conn, "Broad Topic")
    tags.upsert(conn, close_id, "entrepreneurship", 0.6, "inferred")
    tags.upsert(conn, broad_id, "technology", 0.8, "inferred")
    with conn.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO tag_embedding (name, embedding, model) "
            "VALUES (%s, %s::vector, %s)",
            [
                (
                    "entrepreneurship",
                    embeddings.vector_literal(_vector(0.59)),
                    embeddings.MODEL_VERSION,
                ),
                (
                    "technology",
                    embeddings.vector_literal(_vector(0.48)),
                    embeddings.MODEL_VERSION,
                ),
            ],
        )
    query = np.zeros((1, embeddings.DIMENSIONS), dtype=np.float32)
    query[0, 0] = 1
    monkeypatch.setattr(embeddings, "embed_tags", lambda values: query)

    matches = tags.semantic_matches(
        conn, [close_id, broad_id], ["startup"]
    )

    assert matches[close_id]["score"] > matches[broad_id]["score"]
    assert matches[close_id]["concepts"][0]["relatedness"] > 0.8
    assert matches[broad_id]["concepts"][0]["relatedness"] < 0.6


def test_multiple_desired_tags_measure_joint_concept_coverage(conn, monkeypatch):
    # Neutral titles isolate stored-tag aggregation from the independent
    # explicit-title evidence path.
    both_id = _event(conn, "Two Concepts")
    dance_only_id = _event(conn, "One Concept")
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
    assert matches[both_id]["score"] == pytest.approx(
        0.9 * harmonic
    )
    assert matches[dance_only_id]["score"] == 0
    assert [m["query"] for m in matches[both_id]["concepts"]] == [
        "dance", "elegant", "dance + elegant"
    ]
    assert matches[both_id]["concepts"][-1]["joint"] is True
    assert [m["role"] for m in matches[both_id]["concepts"]] == [
        "requested_concept", "requested_concept", "combined_phrase_context",
    ]
    assert matches[both_id]["weakest_concept_query"] == "elegant"
    assert matches[both_id]["weakest_concept_score"] == 0.7
    assert matches[both_id]["combined_context_score"] == 0


def test_multi_concept_score_is_monotonic_as_one_concept_improves(
    conn, monkeypatch,
):
    balanced_weak_id = _event(conn, "Two Weak Concepts")
    stronger_dance_id = _event(conn, "One Improved Concept")
    for event_id, dance_confidence in [
        (balanced_weak_id, 0.2),
        (stronger_dance_id, 0.8),
    ]:
        tags.upsert(conn, event_id, "dance", dance_confidence, "inferred")
        tags.upsert(conn, event_id, "elegant", 0.2, "inferred")
    monkeypatch.setattr(
        embeddings, "embed_tags",
        lambda values: np.zeros(
            (len(values), embeddings.DIMENSIONS), dtype=np.float32
        ),
    )

    scores = tags.semantic_scores(
        conn, [balanced_weak_id, stronger_dance_id], ["dance", "elegant"]
    )

    assert scores[stronger_dance_id] > scores[balanced_weak_id]


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


def test_sql_membership_can_require_every_requested_concept(conn, monkeypatch):
    event_id = _event(conn, "Uneven Concepts")
    tags.upsert(conn, event_id, "singing", 0.8, "inferred")
    tags.upsert(conn, event_id, "movement", 0.2, "inferred")
    monkeypatch.setattr(
        embeddings,
        "embed_tags",
        lambda values: np.zeros(
            (len(values), embeddings.DIMENSIONS), dtype=np.float32
        ),
    )
    params = {}
    aggregate_only, _ = tags.semantic_threshold_sql(
        ["singing", "movement"], 0.25, params, prefix="aggregate_only"
    )
    params["event_id"] = event_id
    assert conn.execute(
        f"SELECT 1 FROM event e WHERE e.id = %(event_id)s AND {aggregate_only}",
        params,
    ).fetchone()

    strict_params = {}
    strict, _ = tags.semantic_threshold_sql(
        ["singing", "movement"], 0.25, strict_params, prefix="strict_each",
        min_concept_match=0.3,
    )
    strict_params["event_id"] = event_id
    assert conn.execute(
        f"SELECT 1 FROM event e WHERE e.id = %(event_id)s AND {strict}",
        strict_params,
    ).fetchone() is None


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


def test_literal_title_concepts_cover_generic_compounds_before_enrichment(
    conn, monkeypatch,
):
    yoga_id = _event(conn, "Yogakurse im Sommer")
    ball_id = _event(conn, "Linzer Maturaball")
    unrelated_id = _event(conn, "Startup Training")
    monkeypatch.setattr(
        embeddings,
        "embed_tags",
        lambda values: np.zeros(
            (len(values), embeddings.DIMENSIONS), dtype=np.float32
        ),
    )

    yoga = tags.semantic_matches(
        conn, [yoga_id, unrelated_id], ["yoga"]
    )
    ball = tags.semantic_matches(
        conn, [ball_id, unrelated_id], ["ball"]
    )

    assert yoga[yoga_id]["score"] == tags.TITLE_EVIDENCE_CONFIDENCE
    assert yoga[yoga_id]["concepts"][0]["origin"] == "title"
    assert yoga[unrelated_id]["score"] == 0
    assert ball[ball_id]["score"] == tags.TITLE_EVIDENCE_CONFIDENCE

    params = {}
    condition, _ = tags.semantic_threshold_sql(
        ["yoga"], 0.8, params, prefix="title_tag"
    )
    rows = conn.execute(
        f"SELECT e.id FROM event e WHERE {condition}", params
    ).fetchall()
    assert {row["id"] for row in rows} == {yoga_id}


def test_title_evidence_is_not_diluted_and_sql_uses_same_token_boundaries(
    conn, monkeypatch,
):
    singing_id = _event(conn, "Singing Circle")
    smart_id = _event(conn, "Smart Fair")
    art_id = _event(conn, "Community Art-Fair")
    tags.upsert(conn, singing_id, "singing", 0.8, "inferred")
    tags.upsert(conn, singing_id, "movement", 0.8, "inferred")
    monkeypatch.setattr(
        embeddings,
        "embed_tags",
        lambda values: np.zeros(
            (len(values), embeddings.DIMENSIONS), dtype=np.float32
        ),
    )

    match = tags.semantic_matches(
        conn, [singing_id], ["singing", "movement"]
    )[singing_id]
    assert match["concepts"][0]["score"] == tags.TITLE_EVIDENCE_CONFIDENCE
    assert match["score"] == pytest.approx(0.8)

    parity_params = {}
    parity_condition, _ = tags.semantic_threshold_sql(
        ["singing", "movement"], 0.8, parity_params,
        prefix="title_parity", min_concept_match=0.8,
    )
    parity_params["id"] = singing_id
    assert conn.execute(
        f"SELECT 1 FROM event e WHERE e.id = %(id)s AND {parity_condition}",
        parity_params,
    ).fetchone()

    title_matches = tags.semantic_matches(
        conn, [smart_id, art_id], ["art fair"]
    )
    assert title_matches[smart_id]["score"] == 0
    assert title_matches[art_id]["score"] == tags.TITLE_EVIDENCE_CONFIDENCE

    params = {}
    condition, _ = tags.semantic_threshold_sql(
        ["art fair"], 1.0, params, prefix="title_boundary"
    )
    params["ids"] = [smart_id, art_id]
    rows = conn.execute(
        f"SELECT e.id FROM event e WHERE e.id = ANY(%(ids)s) AND {condition}",
        params,
    ).fetchall()
    assert {row["id"] for row in rows} == {art_id}


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


def test_inferred_tag_cleanup_removes_only_known_structured_fillers():
    cleaned = tags.clean_estimates([
        {"name": "adult", "confidence": 0.8, "evidence": "Erwachsene"},
        {"name": "female led", "confidence": 0.8, "evidence": "Birgit"},
        {"name": "evening event", "confidence": 0.35, "evidence": None},
        {"name": "intimate audience", "confidence": 0.35, "evidence": None},
        {"name": "linz metro", "confidence": 0.2, "evidence": None},
        {"name": "movement to music", "confidence": 0.8, "evidence": "Bewegen"},
        {"name": "women's run", "confidence": 0.8, "evidence": "Frauenlauf"},
        {"name": "outdoor cinema", "confidence": 0.8, "evidence": "Freiluftkino"},
    ])

    assert [tag["name"] for tag in cleaned] == [
        "movement to music", "outdoor cinema", "women's run",
    ]


def test_specific_tags_do_not_mechanically_synthesize_one_word_parents():
    cleaned = tags.clean_estimates([
        {"name": "mantra singing", "confidence": 0.8, "evidence": "Mantras"},
        {"name": "running club", "confidence": 0.8, "evidence": "Run Crew"},
    ])

    assert [tag["name"] for tag in cleaned] == ["mantra singing", "running club"]


def test_public_tags_expose_safe_evidence_bases_without_quotes(conn):
    event_id = _event(conn, "Singing Circle")
    key = uuid.uuid4().hex
    cached_tags = [
        {"name": "singing circle", "confidence": 0.8,
         "evidence": "Singing Circle"},
        {"name": "movement", "confidence": 0.8,
         "evidence": "Bewegen zu den Klangwelten"},
        {"name": "meditative", "confidence": 0.35, "evidence": None},
    ]
    conn.execute(
        "INSERT INTO enrichment (content_key, attributes) VALUES (%s, %s)",
        (key, Jsonb({"tags": cached_tags})),
    )
    conn.execute(
        "UPDATE event SET inferred = %s WHERE id = %s",
        (Jsonb({"_enrichment": {"content_key": key}}), event_id),
    )
    tags.replace_inferred(conn, event_id, cached_tags)
    tags.upsert(conn, event_id, "community music", 0.6, "source")

    rows = {row["name"]: row for row in tags.public_for_event(conn, event_id)}

    assert rows["singing circle"]["evidence_bases"] == ["title"]
    assert rows["movement"]["evidence_bases"] == ["explicit_text"]
    assert rows["meditative"]["evidence_bases"] == ["world_knowledge"]
    assert rows["community music"]["evidence_bases"] == ["source"]
    assert all("evidence" not in row for row in rows.values())


def test_merged_evidence_bases_survive_inferred_tag_replacement(conn):
    event_id = _event(conn, "Community Music Workshop")
    tags.upsert(conn, event_id, "community music", 0.6, "source")

    def project_inferred(key: str, evidence: str) -> None:
        estimate = {
            "name": "community music", "confidence": 0.8,
            "evidence": evidence,
        }
        conn.execute(
            "INSERT INTO enrichment (content_key, attributes) VALUES (%s, %s)",
            (key, Jsonb({"tags": [estimate]})),
        )
        conn.execute(
            "UPDATE event SET inferred = %s WHERE id = %s",
            (Jsonb({"_enrichment": {"content_key": key}}), event_id),
        )
        tags.replace_inferred(conn, event_id, [estimate])

    project_inferred("merged-evidence-v1", "gemeinsam Musik machen")
    first = tags.public_for_event(conn, event_id)[0]
    assert first["origins"] == ["inferred", "source"]
    assert first["evidence_bases"] == ["source", "explicit_text"]
    assert "evidence" not in first

    project_inferred("merged-evidence-v2", "zusammen musizieren")
    second = tags.public_for_event(conn, event_id)[0]
    assert second["origins"] == ["inferred", "source"]
    assert second["evidence_bases"] == ["source", "explicit_text"]
    assert second["confidence"] == 0.8
    assert "evidence" not in second


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
