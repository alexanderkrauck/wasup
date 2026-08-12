"""Unified confidence-bearing event tags and semantic matching."""

from __future__ import annotations

import re
from collections.abc import Iterable
from uuid import UUID

from eventindex import embeddings

MAX_TAGS_PER_EVENT = 16
MAX_DESIRED_TAGS = 8
MAX_TAG_WORDS = 3
MAX_TAG_LENGTH = 60
MULTI_CONCEPT_SUPPORTS = 2
MAX_JOINT_CONCEPTS = 3
JOINT_CONTEXT_WEIGHT = 0.1
# This is not an inferred event attribute: it is certainty that the literal
# requested concept is present in the event's own title. Keep it above broad
# embedding neighbours so "Yoga am See" cannot rank below "dance training".
TITLE_EVIDENCE_CONFIDENCE = 1.0
MIN_COMPOUND_FRAGMENT_LENGTH = 4

# These are already represented by typed event fields and repeatedly appeared
# as quota-filling inferred tags in production. Keep this deliberately narrow:
# a semantic concept such as "outdoor cinema" or "women's run" must not be
# erased merely because it contains a structured attribute.
_REDUNDANT_INFERRED_TAGS = frozenset({
    "adult", "adults", "adult audience", "female led", "male led",
    "evening event", "morning event", "daytime event", "weekend event",
    "intimate audience", "small audience", "medium audience",
    "large audience", "linz metro", "linz area", "indoor",
})
_EVIDENCE_BASIS_ORDER = (
    "source", "category", "title", "explicit_text", "world_knowledge",
    "unknown",
)


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
        if name is None or name in _REDUNDANT_INFERRED_TAGS:
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


def _words(value: str) -> list[str]:
    """Unicode-aware mechanical tokenization, without a language vocabulary."""
    words: list[str] = []
    current: list[str] = []
    for character in value.casefold():
        if character.isalnum():
            current.append(character)
        elif current:
            words.append("".join(current))
            current = []
    if current:
        words.append("".join(current))
    return words


def _title_evidence(title: str, concept: str) -> str | None:
    """Return explicit title evidence for a literal concept.

    Exact token sequences always count. A single concept of four or more
    characters may also be the prefix or suffix of a title word, which
    handles compounds generically (Yogakurse, Maturaball) without a
    site-, language-, or activity-specific vocabulary.
    """
    title_words = _words(title)
    concept_words = _words(concept)
    if not title_words or not concept_words:
        return None
    width = len(concept_words)
    for index in range(len(title_words) - width + 1):
        window = title_words[index:index + width]
        if window == concept_words:
            return " ".join(window)
    if width == 1 and len(concept_words[0]) >= MIN_COMPOUND_FRAGMENT_LENGTH:
        concept_word = concept_words[0]
        for title_word in title_words:
            if (
                title_word.startswith(concept_word)
                or title_word.endswith(concept_word)
            ):
                return title_word
    return None


def _query_specs(desired: list[str]) -> list[dict]:
    """Requested concepts plus one order-invariant joint context.

    Short embeddings are good at broad relatedness but single words are
    polysemous (live examples: salsa sauce versus salsa dance) and can be
    embedding hubs. For two or three requested concepts, the combined phrase
    supplies the missing word sense. It may confirm or reduce joint coverage,
    but cannot compensate for a weak requested concept.
    """
    specs = [
        {"query": name, "embedding_text": name, "joint": False}
        for name in desired
    ]
    if 1 < len(desired) <= MAX_JOINT_CONCEPTS:
        specs.append({
            "query": " + ".join(desired),
            "embedding_text": " ".join(sorted(desired)),
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
    rows = tx.execute(
        """
        SELECT et.name, et.confidence, et.origins, e.title,
               cached.tag->>'evidence' AS inferred_evidence,
               cached.tag IS NOT NULL AS has_cached_inferred_tag
        FROM event_tag et
        JOIN event e ON e.id = et.event_id
        LEFT JOIN enrichment en
          ON en.content_key = e.inferred->'_enrichment'->>'content_key'
        LEFT JOIN LATERAL (
            SELECT tag
            FROM jsonb_array_elements(coalesce(en.attributes->'tags', '[]')) tag
            WHERE tag->>'name' = et.name
            LIMIT 1
        ) cached(tag) ON true
        WHERE et.event_id = %s
        ORDER BY et.confidence DESC, et.name
        LIMIT %s
        """,
        (event_id, MAX_TAGS_PER_EVENT),
    ).fetchall()
    public = []
    for row in rows:
        bases = set()
        for origin in row["origins"]:
            if origin in {"source", "category"}:
                bases.add(origin)
            elif origin == "inferred":
                if not row["has_cached_inferred_tag"]:
                    bases.add("unknown")
                elif evidence := str(row["inferred_evidence"] or "").strip():
                    bases.add(
                        "title"
                        if evidence.casefold() in row["title"].casefold()
                        else "explicit_text"
                    )
                else:
                    bases.add("world_knowledge")
            else:
                bases.add("unknown")
        public.append({
            "name": row["name"],
            "confidence": row["confidence"],
            "origins": row["origins"],
            "evidence_bases": [
                basis for basis in _EVIDENCE_BASIS_ORDER if basis in bases
            ],
        })
    return public


def semantic_matches(
    tx, event_ids: Iterable[UUID], desired: list[str]
) -> dict[UUID, dict]:
    """Joint concept coverage with per-concept evidence for agent responses.

    Every requested concept retains its own confidence-bearing evidence.
    Non-exact concepts average the two strongest supporting event tags instead
    of trusting one accidental embedding neighbour; exact evidence keeps its
    full certainty. Requested concepts combine with a harmonic mean, which
    nonlinearly penalizes a weak concept while remaining monotonic: stronger
    evidence for any concept can never reduce the result. Joint-phrase context
    can confirm or reduce that coverage, never inflate it.
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
    title_confidences: dict[UUID, list[float]] = {
        event_id: [0.0 for _ in desired] for event_id in event_ids
    }
    titles = {
        row["id"]: row["title"]
        for row in tx.execute(
            "SELECT id, title FROM event WHERE id = ANY(%s)", (event_ids,)
        )
    }
    for event_id, title in titles.items():
        for index, concept in enumerate(desired):
            title_word = _title_evidence(title or "", concept)
            if title_word is None:
                continue
            title_confidences[event_id][index] = TITLE_EVIDENCE_CONFIDENCE
            evidence[event_id][index].append({
                "score": TITLE_EVIDENCE_CONFIDENCE,
                "event_tag": title_word,
                "tag_confidence": TITLE_EVIDENCE_CONFIDENCE,
                "relatedness": 1.0,
                "origin": "title",
            })
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
                relatedness = embeddings.retrieval_relatedness(row[f"sim_{index}"])
            score = float(row["confidence"]) * relatedness
            if score <= 0:
                continue
            evidence[row["event_id"]][index].append({
                "score": score,
                "event_tag": row["name"],
                "tag_confidence": float(row["confidence"]),
                "relatedness": relatedness,
                "origin": "event_tag",
            })

    support_limit = 1 if len(desired) == 1 else MULTI_CONCEPT_SUPPORTS
    result = {}
    for event_id, by_spec in evidence.items():
        concepts = []
        for index, (spec, candidates) in enumerate(zip(specs, by_spec)):
            supports = sorted(
                candidates,
                key=lambda item: (-item["score"], item["event_tag"]),
            )[:support_limit]
            exact = (
                exact_confidences[event_id][index]
                if not spec["joint"] else 0.0
            )
            semantic_support = (
                sum(item["score"] for item in supports) / len(supports)
                if supports else 0.0
            )
            title_confidence = (
                title_confidences[event_id][index]
                if not spec["joint"] else 0.0
            )
            score = max(exact, semantic_support, title_confidence)
            best = supports[0] if supports else {
                "event_tag": None, "tag_confidence": None, "relatedness": 0.0,
                "origin": None,
            }
            concepts.append({
                "query": spec["query"],
                "score": score,
                "event_tag": best["event_tag"],
                "tag_confidence": best["tag_confidence"],
                "relatedness": best["relatedness"],
                "origin": best["origin"],
                "supports": supports,
                "joint": spec["joint"],
                "role": (
                    "combined_phrase_context"
                    if spec["joint"] else "requested_concept"
                ),
            })
        individual_scores = [
            concept["score"] for concept in concepts[:len(desired)]
        ]
        if len(desired) == 1:
            semantic_score = individual_scores[0]
        elif any(score <= 0 for score in individual_scores):
            semantic_score = 0.0
        else:
            harmonic = len(individual_scores) / sum(
                1.0 / score for score in individual_scores
            )
            coverage = harmonic
            if len(specs) > len(desired):
                joint_score = concepts[-1]["score"]
                semantic_score = (
                    (1 - JOINT_CONTEXT_WEIGHT) * coverage
                    + JOINT_CONTEXT_WEIGHT * min(coverage, joint_score)
                )
            else:
                semantic_score = coverage
        weakest = min(
            concepts[:len(desired)],
            key=lambda concept: (concept["score"], concept["query"]),
        )
        result[event_id] = {
            "score": semantic_score,
            "concepts": concepts,
            "weakest_concept_score": weakest["score"],
            "weakest_concept_query": weakest["query"],
            "combined_context_score": (
                concepts[-1]["score"]
                if len(specs) > len(desired) else None
            ),
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
    desired: list[str], min_match: float, params: dict, *, prefix: str,
    min_concept_match: float | None = None,
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
    title_scores = []
    for index, (spec, vector) in enumerate(zip(specs, vectors)):
        name_key = f"{prefix}_name_{index}"
        vector_key = f"{prefix}_vector_{index}"
        params[name_key] = spec["embedding_text"]
        params[vector_key] = embeddings.vector_literal(vector)
        relations.append(
            "CASE WHEN et.name = %({name})s THEN 1.0 "
            "WHEN te.embedding IS NULL THEN 0.0 ELSE "
            "power(1.0 / (1.0 + exp(({center} - "
            "(1.0 - (te.embedding <=> %({vector})s::vector))) "
            "/ {temperature})), {power}) END".format(
                name=name_key,
                vector=vector_key,
                center=embeddings.CALIBRATION_CENTER,
                temperature=embeddings.CALIBRATION_TEMPERATURE,
                power=embeddings.RELATEDNESS_FOCUS_POWER,
            )
        )
        if index < len(desired):
            title_key = f"{prefix}_title_{index}"
            params[title_key] = spec["embedding_text"]
            if " " in spec["embedding_text"]:
                # Match the same contiguous token sequence as
                # _title_evidence. A bare substring made "art fair" match
                # "Smart Fair" on SQL surfaces but not in Python.
                pattern_key = f"{prefix}_title_pattern_{index}"
                words = _words(spec["embedding_text"])
                params[pattern_key] = (
                    "(^|[^[:alnum:]])"
                    + "[^[:alnum:]]+".join(re.escape(word) for word in words)
                    + "($|[^[:alnum:]])"
                )
                matched = f"e.title ~* %({pattern_key})s"
            else:
                # regexp_split is only mechanical Unicode word separation.
                # Prefix/suffix matching makes compounds searchable while the
                # four-character floor avoids short accidental fragments.
                matched = (
                    "EXISTS (SELECT 1 FROM regexp_split_to_table("
                    "lower(e.title), '[^[:alnum:]]+') AS title_word "
                    f"WHERE title_word = %({title_key})s OR ("
                    f"length(%({title_key})s) >= {MIN_COMPOUND_FRAGMENT_LENGTH} "
                    f"AND (starts_with(title_word, %({title_key})s) "
                    f"OR right(title_word, length(%({title_key})s)) "
                    f"= %({title_key})s)))"
                )
            title_scores.append(
                f"CASE WHEN {matched} THEN {TITLE_EVIDENCE_CONFIDENCE} "
                "ELSE 0.0 END"
            )
    model_key = f"{prefix}_model"
    match_key = f"{prefix}_min_match"
    params[model_key] = embeddings.MODEL_VERSION
    params[match_key] = min_match
    concept_match_key = f"{prefix}_min_concept_match"
    if min_concept_match is not None:
        params[concept_match_key] = min_concept_match
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
    concept_scores = []
    for index in range(len(relations)):
        averaged = (
            "coalesce((SELECT avg(value) FROM "
            f"unnest(scores_{index}[1:{support_limit}]) AS support(value)), 0.0)"
        )
        concept_scores.append(
            f"greatest(exact_{index}, {averaged}, {title_scores[index]})"
            if index < len(desired) else averaged
        )
    if len(desired) == 1:
        final_score = concept_scores[0]
    else:
        individual_scores = concept_scores[:len(desired)]
        any_zero = " OR ".join(f"({score}) <= 0" for score in individual_scores)
        reciprocal_sum = " + ".join(
            f"(1.0 / ({score}))" for score in individual_scores
        )
        harmonic = f"{len(desired)}::float / ({reciprocal_sum})"
        coverage = (
            f"CASE WHEN {any_zero} THEN 0.0 "
            f"ELSE ({harmonic}) END"
        )
        if len(specs) > len(desired):
            joint_score = concept_scores[-1]
            final_score = (
                f"(1 - {JOINT_CONTEXT_WEIGHT}) * ({coverage}) + "
                f"{JOINT_CONTEXT_WEIGHT} * least(({coverage}), ({joint_score}))"
            )
        else:
            final_score = coverage
    weakest_score = (
        concept_scores[0]
        if len(desired) == 1
        else "least(" + ", ".join(concept_scores[:len(desired)]) + ")"
    )
    membership = (
        "round((" + final_score + ")::numeric, 4) "
        + f">= %({match_key})s"
    )
    if min_concept_match is not None:
        membership += (
            " AND round((" + weakest_score + ")::numeric, 4) "
            + f">= %({concept_match_key})s"
        )
    return (
        "(SELECT (" + membership + ") "
        "FROM (SELECT " + ", ".join(score_arrays + exact_columns) + " "
        "FROM event_tag et LEFT JOIN tag_embedding te ON te.name = et.name "
        f"AND te.model = %({model_key})s WHERE et.event_id = e.id) ranked)",
        desired,
    )
