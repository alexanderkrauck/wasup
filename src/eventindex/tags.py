"""Unified confidence-bearing event tags and semantic matching."""

from __future__ import annotations

from collections.abc import Iterable
from uuid import UUID

from eventindex import embeddings

MAX_TAGS_PER_EVENT = 16
MAX_DESIRED_TAGS = 8
MAX_TAG_WORDS = 3
MAX_TAG_LENGTH = 60


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


def semantic_scores(
    tx, event_ids: Iterable[UUID], desired: list[str]
) -> dict[UUID, float]:
    """Best certainty-weighted relation per event; no tag-count reward."""
    event_ids = list(dict.fromkeys(event_ids))
    desired = clean_desired(desired)
    if not event_ids or not desired:
        return {}
    vectors = embeddings.embed_tags(desired)
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
    scores: dict[UUID, float] = {event_id: 0.0 for event_id in event_ids}
    for row in rows:
        best_relation = 0.0
        for index, query in enumerate(desired):
            if row["name"] == query:
                relatedness = 1.0
            elif row[f"sim_{index}"] is None:
                relatedness = 0.0
            else:
                relatedness = embeddings.calibrated_relatedness(row[f"sim_{index}"])
            best_relation = max(best_relation, relatedness)
        score = float(row["confidence"]) * best_relation
        scores[row["event_id"]] = max(scores[row["event_id"]], score)
    return scores


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
    vectors = embeddings.embed_tags(desired)
    relations = []
    for index, (name, vector) in enumerate(zip(desired, vectors)):
        name_key = f"{prefix}_name_{index}"
        vector_key = f"{prefix}_vector_{index}"
        params[name_key] = name
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
    return (
        "EXISTS (SELECT 1 FROM event_tag et "
        "LEFT JOIN tag_embedding te ON te.name = et.name "
        f"AND te.model = %({model_key})s "
        "WHERE et.event_id = e.id AND et.confidence * GREATEST("
        + ", ".join(relations)
        + f") >= %({match_key})s)",
        desired,
    )
