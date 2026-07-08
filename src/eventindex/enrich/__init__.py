"""Inferred-attribute enrichment (§8 / H5): priors with humility.

The category prior is the baseline; the LLM may ADJUST it only when the
event's own text gives explicit evidence ("Seniorencafé", "ab 18",
"Anfänger willkommen"), and must say what the evidence was. Confidence is
capped at 0.8 - these are estimates by construction, served labeled.

Results are cached by content hash: canon rebuilds re-apply the cache for
free; only genuinely new/changed events cost an LLM call.
"""

import hashlib
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from eventindex import config, llm

CONFIDENCE_CAP = 0.8
# Confidence tiers (Alexander 2026-07-06: ALWAYS estimate; confidence says
# how much it's a guess): ~0.2 pure world-knowledge guess, ~0.35 typical for
# this kind of event, up to 0.8 with explicit textual evidence.
GUESS_CONFIDENCE = 0.2


class _Est(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float | None
    confidence: float
    evidence: str | None = Field(description="verbatim text snippet, or null if prior only")


class _BoolEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: bool | None
    confidence: float
    evidence: str | None


class Enrichment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # descriptions live in the prompt: strict schema mode forbids
    # annotations on $ref fields
    age_min: _Est
    age_max: _Est
    gender_split: _Est
    expected_attendance: _Est
    language: Literal["de", "en", "other"] | None
    kid_friendly: _BoolEst
    newcomer_friendly: _BoolEst
    outdoor: _BoolEst
    solo_friendly: _BoolEst
    interaction_structure: Literal["none", "optional", "built_in"] | None
    energy: Literal["low", "medium", "high"] | None
    vibe_tags: list[str] = Field(description="3-6 short lowercase vibe words")


# bump when the Enrichment schema gains fields: old cache rows lack them, so
# a version change re-enriches the corpus (cheap: ~EUR 0.0003/event)
SCHEMA_VERSION = 2


def content_key(event: dict) -> str:
    parts = "|".join([
        f"v{SCHEMA_VERSION}",
        event.get("title") or "", (event.get("description") or "")[:500],
        ",".join(event.get("category") or []), str(event.get("venue_name") or ""),
    ])
    return hashlib.md5(parts.encode()).hexdigest()


def _prior_for(tx, categories: list[str]) -> dict:
    if not categories:
        return {}
    row = tx.execute(
        "SELECT priors FROM category_priors WHERE category = %s", (categories[0],)
    ).fetchone()
    return row["priors"] if row else {}


def enrich_event(tx, event: dict, job_id=None) -> dict:
    """Compute (or fetch cached) inferred attributes for one canonical event.
    Returns the attributes dict."""
    key = content_key(event)
    cached = tx.execute(
        "SELECT attributes FROM enrichment WHERE content_key = %s", (key,)
    ).fetchone()
    if cached:
        return cached["attributes"]

    prior = _prior_for(tx, event.get("category") or [])
    result = llm.complete(
        tx,
        "Estimate audience attributes for this Linz event. ALWAYS give your "
        "best estimate - null only if an attribute is truly inapplicable. "
        "Confidence encodes how much it is a guess: "
        f"~{GUESS_CONFIDENCE} = pure world-knowledge guess, ~0.35 = typical "
        "for this kind of event (use the category prior if given), up to "
        f"{CONFIDENCE_CAP} ONLY with explicit textual evidence (quote it in "
        "evidence).\n"
        "gender_split: 0=all male .. 1=all female. newcomer_friendly: open to "
        "strangers vs members-only circles. solo_friendly: normal to attend "
        "alone (a run club: yes; a couples dance course: no). "
        "interaction_structure: does the FORMAT make attendees interact - "
        "'built_in' = rotation/teams/pair work forces it (Salsa mixer, pub "
        "quiz with assigned teams, language tandem), 'optional' = easy but "
        "not forced (Stammtisch, board game cafe), 'none' = you can stay "
        "silent throughout (concert, cinema, lecture).\n\n"
        f"CATEGORY PRIOR: {prior}\n"
        f"TITLE: {event.get('title')}\n"
        f"DESCRIPTION: {(event.get('description') or '')[:1200]}\n"
        f"CATEGORY: {event.get('category')}\nVENUE: {event.get('venue_name')}\n"
        f"PRICE: {event.get('price_min')}-{event.get('price_max')}",
        Enrichment,
        job_id=job_id,
    )
    attributes = result.model_dump()
    for entry in attributes.values():  # the cap is code, not model discipline
        if isinstance(entry, dict) and "confidence" in entry:
            entry["confidence"] = min(entry["confidence"], CONFIDENCE_CAP)
    tx.execute(
        "INSERT INTO enrichment (content_key, attributes, model) VALUES (%s, %s, %s) "
        "ON CONFLICT (content_key) DO NOTHING",
        (key, Jsonb(attributes), config.MODEL_MINI),
    )
    return attributes


def apply_to_event(tx, event_id, attributes: dict) -> None:
    """Write attributes into the typed §2 columns + the inferred jsonb."""
    age_min = attributes.get("age_min", {}).get("value")
    age_max = attributes.get("age_max", {}).get("value")
    age_conf = min(
        attributes.get("age_min", {}).get("confidence", 0),
        attributes.get("age_max", {}).get("confidence", 0),
    )
    tx.execute(
        """
        UPDATE event SET
            expected_age_range = CASE
                WHEN %(age_min)s::int IS NULL OR %(age_max)s::int IS NULL THEN NULL
                ELSE int4range(%(age_min)s, %(age_max)s, '[]') END,
            expected_age_range_confidence = %(age_conf)s,
            expected_gender_split = %(gender)s,
            expected_gender_split_confidence = %(gender_conf)s,
            expected_attendance = %(attendance)s,
            expected_attendance_confidence = %(attendance_conf)s,
            inferred = %(inferred)s
        WHERE id = %(id)s
        """,
        {
            "id": event_id,
            "age_min": int(age_min) if age_min is not None else None,
            "age_max": int(age_max) if age_max is not None else None,
            "age_conf": age_conf,
            "gender": attributes.get("gender_split", {}).get("value"),
            "gender_conf": attributes.get("gender_split", {}).get("confidence"),
            "attendance": (
                int(a) if (a := attributes.get("expected_attendance", {}).get("value"))
                is not None else None
            ),
            "attendance_conf": attributes.get("expected_attendance", {}).get("confidence"),
            "inferred": Jsonb({
                k: attributes[k] for k in
                ("language", "kid_friendly", "newcomer_friendly", "outdoor",
                 "solo_friendly", "interaction_structure", "energy",
                 "vibe_tags") if k in attributes
            }),
        },
    )
