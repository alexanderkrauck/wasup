"""Unified confidence-bearing event tags and semantic matching."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from eventindex import embeddings

MAX_TAGS_PER_EVENT = 16
MAX_DESIRED_TAGS = 8
MAX_TAG_WORDS = 3
MAX_TAG_LENGTH = 60
MULTI_CONCEPT_SUPPORTS = 2
MAX_JOINT_CONCEPTS = 3
MULTI_SEMANTIC_WEIGHT = 0.75
MULTI_EXACT_COVERAGE_WEIGHT = 0.25


def clean_name(value: str) -> str | None:
    name = embeddings.normalize_tag(str(value))
    if (
        not name
        or len(name) > MAX_TAG_LENGTH
        or len(name.split()) > MAX_TAG_WORDS
        or "(" in name
        or ")" in name
        or "\n" in name
    ):
        return None
    return name


def clean_estimates(values: Iterable[dict]) -> list[dict]:
    """Validate model tag output and merge duplicate names by confidence."""
    merged: dict[str, dict] = {}
    for value in values:
        name = clean_name(value.get("name", ""))
        if name is None:
            continue
        confidence = min(1.0, max(0.0, float(value.get("confidence", 0))))
        candidate = {
            "name": name,
            "confidence": confidence,
            "evidence": value.get("evidence"),
        }
        if name not in merged or confidence > merged[name]["confidence"]:
            merged[name] = candidate
    return sorted(
        merged.values(), key=lambda tag: (-tag["confidence"], tag["name"])
    )[:MAX_TAGS_PER_EVENT]


def clean_desired(values: Iterable[str]) -> list[str]:
    """Validate and deduplicate public tag-query concepts."""
    cleaned = []
    for value in values:
        name = clean_name(value)
        if name is None:
            raise ValueError(
                f"tag concepts must be 1-{MAX_TAG_WORDS} words and at most "
                f"{MAX_TAG_LENGTH} characters"
            )
        if name not in cleaned:
            cleaned.append(name)
    if len(cleaned) > MAX_DESIRED_TAGS:
        raise ValueError(f"at most {MAX_DESIRED_TAGS} tag concepts are allowed")
    return cleaned


def _query_specs(desired: list[str]) -> list[dict]:
    """Requested concepts plus one order-invariant joint context.

    Short embeddings are good at broad relatedness but single words are
    polysemous (live examples: salsa sauce versus salsa dance) and can be
    embedding hubs. For two or three requested concepts, the combined phrase
    supplies the missing word sense. It receives half of the final weight;
    the individual concepts retain the other half and their own evidence.
    """
    specs = [
        {"query": name, "embedding_text": name, "weight": 1, "joint": False}
        for name in desired
    ]
    if 1 < len(desired) <= MAX_JOINT_CONCEPTS:
        specs.append({
            "query": " + ".join(desired),
            "embedding_text": " ".join(sorted(desired)),
            "weight": len(desired),
            "joint": True,
        })
    return specs


def upsert(tx, event_id: UUID, name: str, confidence: float, origin: str) -> None:
    clean = clean_name(name)
    if clean is None:
        return
    confidence = min(1.0, max(0.0, float(confidence)))
    tx.execute(
        """
        INSERT INTO event_tag (
            event_id, name, confidence, origins, origin_confidences
        )
        VALUES (%s, %s, %s, ARRAY[%s]::text[], jsonb_build_object(%s::text, %s::float))
        ON CONFLICT (event_id, name) DO UPDATE SET
            confidence = (
                SELECT max(value::float)
                FROM jsonb_each_text(
                    event_tag.origin_confidences || excluded.origin_confidences
                )
            ),
            origins = ARRAY(
                SELECT DISTINCT value
                FROM unnest(event_tag.origins || excluded.origins) AS value
                ORDER BY value
            ),
            origin_confidences = (
                event_tag.origin_confidences || excluded.origin_confidences
            )
        """,
        (event_id, clean, confidence, origin, origin, confidence),
    )


def add_canonical(
    tx,
    event_id: UUID,
    source_tags: Iterable[str],
    source_confidence: float,
    categories: Iterable[str],
    category_confidence: float,
) -> None:
    for name in source_tags:
        upsert(tx, event_id, name, source_confidence, "source")
    for name in categories:
        upsert(tx, event_id, name, category_confidence, "category")


def replace_inferred(tx, event_id: UUID, estimates: Iterable[dict]) -> None:
    estimates = list(estimates)
    # Re-enrichment replaces only the inferred contribution. Source/category
    # origins remain part of the same row and therefore cannot be erased.
    tx.execute(
        "DELETE FROM event_tag WHERE event_id = %s "
        "AND origin_confidences ? 'inferred' "
        "AND origin_confidences - 'inferred' = '{}'::jsonb",
        (event_id,),
    )
    tx.execute(
        "UPDATE event_tag SET origins = array_remove(origins, 'inferred'), "
        "origin_confidences = origin_confidences - 'inferred', "
        "confidence = (SELECT max(value::float) FROM jsonb_each_text("
        "origin_confidences - 'inferred')) "
        "WHERE event_id = %s AND origin_confidences ? 'inferred'",
        (event_id,),
    )
    for tag in estimates:
        upsert(tx, event_id, tag["name"], tag["confidence"], "inferred")


def public_for_event(tx, event_id: UUID) -> list[dict]:
    return tx.execute(
        "SELECT name, confidence, origins FROM event_tag "
        "WHERE event_id = %s ORDER BY confidence DESC, name LIMIT %s",
        (event_id, MAX_TAGS_PER_EVENT),
    ).fetchall()


def semantic_matches(
    tx, event_ids: Iterable[UUID], desired: list[str]
) -> dict[UUID, dict]:
    """Joint concept coverage with per-concept evidence for agent responses.

    Every requested concept retains its own confidence-bearing evidence.
    Multi-concept queries average the two strongest supporting event tags
    instead of trusting one accidental embedding neighbour. The semantic
    composition is blended with exact requested-concept coverage: symmetric
    embeddings know that salsa and dance are related, but exact evidence is
    what distinguishes "salsa" from an arbitrary kind of dance.
    """
    event_ids = list(dict.fromkeys(event_ids))
    desired = clean_desired(desired)
    if not event_ids or not desired:
        return {}
    specs = _query_specs(desired)
    vectors = embeddings.embed_tags([
        spec["embedding_text"] for spec in specs
    ])
    params: dict = {"event_ids": event_ids}
    sim_columns = []
    for index, vector in enumerate(vectors):
        key = f"query_vector_{index}"
        params[key] = embeddings.vector_literal(vector)
        sim_columns.append(
            f"1 - (te.embedding <=> %({key})s::vector) AS sim_{index}"
        )
    rows = tx.execute(
        "SELECT et.event_id, et.name, et.confidence, "
        + ", ".join(sim_columns)
        + " FROM event_tag et LEFT JOIN tag_embedding te ON te.name = et.name "
          "AND te.model = %(model)s WHERE et.event_id = ANY(%(event_ids)s)",
        params | {"model": embeddings.MODEL_VERSION},
    ).fetchall()
    evidence: dict[UUID, list[list[dict]]] = {
        event_id: [
            [] for _ in specs
        ]
        for event_id in event_ids
    }
    exact_confidences: dict[UUID, list[float]] = {
        event_id: [0.0 for _ in desired] for event_id in event_ids
    }
    for row in rows:
        for index, spec in enumerate(specs):
            if row["name"] == spec["embedding_text"]:
                relatedness = 1.0
                if not spec["joint"]:
                    exact_confidences[row["event_id"]][index] = max(
                        exact_confidences[row["event_id"]][index],
                        float(row["confidence"]),
                    )
            elif row[f"sim_{index}"] is None:
                relatedness = 0.0
            else:
                relatedness = embeddings.calibrated_relatedness(row[f"sim_{index}"])
            score = float(row["confidence"]) * relatedness
            if score <= 0:
                continue
            evidence[row["event_id"]][index].append({
                "score": score,
                "event_tag": row["name"],
                "tag_confidence": float(row["confidence"]),
                "relatedness": relatedness,
            })

    support_limit = 1 if len(desired) == 1 else MULTI_CONCEPT_SUPPORTS
    result = {}
    for event_id, by_spec in evidence.items():
        concepts = []
        for spec, candidates in zip(specs, by_spec):
            supports = sorted(
                candidates,
                key=lambda item: (-item["score"], item["event_tag"]),
            )[:support_limit]
            score = (
                sum(item["score"] for item in supports) / len(supports)
                if supports else 0.0
            )
            best = supports[0] if supports else {
                "event_tag": None, "tag_confidence": None, "relatedness": 0.0,
            }
            concepts.append({
                "query": spec["query"],
                "score": score,
                "event_tag": best["event_tag"],
                "tag_confidence": best["tag_confidence"],
                "relatedness": best["relatedness"],
                "supports": supports,
                "joint": spec["joint"],
            })
        total_weight = sum(spec["weight"] for spec in specs)
        semantic_score = sum(
            concept["score"] * spec["weight"]
            for concept, spec in zip(concepts, specs)
        ) / total_weight
        exact_coverage = sum(exact_confidences[event_id]) / len(desired)
        result[event_id] = {
            "score": (
                MULTI_SEMANTIC_WEIGHT * semantic_score
                + MULTI_EXACT_COVERAGE_WEIGHT * exact_coverage
                if len(desired) > 1 else semantic_score
            ),
            "concepts": concepts,
        }
    return result


def semantic_scores(
    tx, event_ids: Iterable[UUID], desired: list[str]
) -> dict[UUID, float]:
    """Aggregate joint-concept scores for ranking and filtering."""
    return {
        event_id: match["score"]
        for event_id, match in semantic_matches(tx, event_ids, desired).items()
    }


def semantic_threshold_sql(
    desired: list[str], min_match: float, params: dict, *, prefix: str
) -> tuple[str, list[str]]:
    """Build a bounded SQL membership predicate for chronological surfaces.

    `/v1/query` scores an already capped candidate pool in Python. Calendar
    and cursor listings instead need semantic membership before SQL LIMIT;
    otherwise a selective tag could force every future occurrence into the
    application on each subscription refresh.
    """
    desired = clean_desired(desired)
    if not desired:
        return "FALSE", desired
    specs = _query_specs(desired)
    vectors = embeddings.embed_tags([
        spec["embedding_text"] for spec in specs
    ])
    relations = []
    for index, (spec, vector) in enumerate(zip(specs, vectors)):
        name_key = f"{prefix}_name_{index}"
        vector_key = f"{prefix}_vector_{index}"
        params[name_key] = spec["embedding_text"]
        params[vector_key] = embeddings.vector_literal(vector)
        relations.append(
            "CASE WHEN et.name = %({name})s THEN 1.0 "
            "WHEN te.embedding IS NULL THEN 0.0 ELSE "
            "1.0 / (1.0 + exp(({center} - "
            "(1.0 - (te.embedding <=> %({vector})s::vector))) "
            "/ {temperature})) END".format(
                name=name_key,
                vector=vector_key,
                center=embeddings.CALIBRATION_CENTER,
                temperature=embeddings.CALIBRATION_TEMPERATURE,
            )
        )
    model_key = f"{prefix}_model"
    match_key = f"{prefix}_min_match"
    params[model_key] = embeddings.MODEL_VERSION
    params[match_key] = min_match
    support_limit = 1 if len(desired) == 1 else MULTI_CONCEPT_SUPPORTS
    score_arrays = [
        "array_agg(et.confidence * ({relation}) "
        "ORDER BY et.confidence * ({relation}) DESC) "
        "FILTER (WHERE et.confidence * ({relation}) > 0) "
        "AS scores_{index}".format(
            relation=relation, index=index,
        )
        for index, relation in enumerate(relations)
    ]
    exact_columns = [
        "coalesce(max(et.confidence) FILTER "
        f"(WHERE et.name = %({prefix}_name_{index})s), 0.0) "
        f"AS exact_{index}"
        for index in range(len(desired))
    ]
    concept_scores = [
        "coalesce((SELECT avg(value) FROM "
        f"unnest(scores_{index}[1:{support_limit}]) AS support(value)), 0.0)"
        for index in range(len(relations))
    ]
    weighted_score = " + ".join(
        f"({score}) * {spec['weight']}"
        for score, spec in zip(concept_scores, specs)
    )
    total_weight = sum(spec["weight"] for spec in specs)
    semantic_score = f"(({weighted_score}) / {total_weight})"
    if len(desired) > 1:
        exact_coverage = (
            "(" + " + ".join(
                f"exact_{index}" for index in range(len(desired))
            ) + f") / {len(desired)}"
        )
        final_score = (
            f"{MULTI_SEMANTIC_WEIGHT} * {semantic_score} + "
            f"{MULTI_EXACT_COVERAGE_WEIGHT} * ({exact_coverage})"
        )
    else:
        final_score = semantic_score
    return (
        "(SELECT round((" + final_score + ")::numeric, 4) "
        "FROM (SELECT " + ", ".join(score_arrays + exact_columns) + " "
        "FROM event_tag et LEFT JOIN tag_embedding te ON te.name = et.name "
        f"AND te.model = %({model_key})s WHERE et.event_id = e.id) ranked) "
        + f">= %({match_key})s",
        desired,
    )
