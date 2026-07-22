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

from eventindex import config, llm, tags as tag_store

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


class _TimeEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | None = Field(description="HH:MM, 24h local, or null")
    confidence: float
    evidence: str | None


class _LanguageEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Literal["de", "en", "other"] | None
    confidence: float
    evidence: str | None


class _TextEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | None
    confidence: float
    evidence: str | None


class _PriceEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: float | None
    max: float | None
    currency: Literal["EUR"] | None
    confidence: float
    evidence: str | None


class _TagEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
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
    language: _LanguageEst
    kid_friendly: _BoolEst
    newcomer_friendly: _BoolEst
    outdoor: _BoolEst
    solo_friendly: _BoolEst
    interaction_structure: Literal["none", "optional", "built_in"] | None
    energy: Literal["low", "medium", "high"] | None
    sex_service_context: _BoolEst
    tags: list[_TagEst] = Field(
        description="6-12 useful event concepts, each 1-3 lowercase words "
        "with its own confidence; no synonyms, translations, or commentary")
    venue: _TextEst
    stated_price: _PriceEst
    start_time: _TimeEst


# bump when the Enrichment schema gains fields: old cache rows lack them, so
# a version change re-enriches the corpus (cheap: ~EUR 0.0003/event)
SCHEMA_VERSION = 5


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


def venue_override(event: dict, attributes: dict) -> dict:
    """Curated venue facts beat estimates (Alexander 2026-07-13): an event
    at a flagged commercial sex establishment ALWAYS carries
    sex_service_context, however innocuous its own text - "Football Lounge
    Nights" says nothing, the venue (Villa Ostende) says everything, and
    the mini model cannot be trusted to know every Etablissement. Applied
    outside the cache so flagging a venue acts immediately."""
    if event.get("venue_sex_service"):
        attributes["sex_service_context"] = {
            "value": True, "confidence": CONFIDENCE_CAP,
            "evidence": "venue is a curated commercial sex establishment",
        }
    return attributes


def enrich_event(tx, event: dict, job_id=None) -> dict:
    """Compute (or fetch cached) inferred attributes for one canonical event.
    Returns the attributes dict."""
    key = content_key(event)
    cached = tx.execute(
        "SELECT attributes FROM enrichment WHERE content_key = %s", (key,)
    ).fetchone()
    if cached:
        return venue_override(event, cached["attributes"])

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
        "silent throughout (concert, cinema, lecture). "
        "language: infer the language attendees will need from the event text "
        "and context, with confidence like every other estimate. "
        "tags: provide 6-12 distinct, useful concepts covering activity/topic, "
        "format, audience, and setting. Each tag is 1-3 lowercase words. "
        "Do not emit generic tags like 'event' or 'linz', commentary, duplicate "
        "synonyms, or translations of the same concept. A tag may use world "
        "knowledge at low confidence; quote evidence when explicit. "
        "venue: only a public venue/organization name explicitly present in "
        "TITLE or DESCRIPTION; never guess and never return an address. "
        "stated_price: only a price explicitly present in TITLE or DESCRIPTION; "
        "use EUR and min=max for a single price, 0 for explicitly free, and "
        "null values when unstated. Never estimate a typical price. "
        "start_time: the typical LOCAL start time (HH:MM) for this kind of "
        "event - used only when the source stated no time; estimate from "
        "the event type (Sunday mass ~09:30, club night ~23:00, "
        "Vernissage ~19:00). "
        "sex_service_context: the event happens at a commercial sex "
        "establishment (Bordell, Laufhaus, strip club, swinger club, erotic "
        "massage studio) or advertises sexual services - guests encounter "
        "sex work as part of the venue's regular operation. NOT true merely "
        "for 18+ parties, regular nightclubs, burlesque/drag shows in "
        "theatres, or queer events: adult-only or risqué aesthetics alone "
        "do not qualify.\n\n"
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
    _sanity_clamp(attributes)
    tx.execute(
        "INSERT INTO enrichment (content_key, attributes, model) VALUES (%s, %s, %s) "
        "ON CONFLICT (content_key) DO NOTHING",
        (key, Jsonb(attributes), config.MODEL_MINI),
    )
    return venue_override(event, attributes)


_TIME_RE = __import__("re").compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _sanity_clamp(attributes: dict) -> None:
    """Deterministic guards (audit A11: age range [18,7251), attendance 0,
    LLM commentary leaking into tags). Estimates stay estimates, but
    impossible values become unknown."""
    lo = attributes.get("age_min", {}).get("value")
    hi = attributes.get("age_max", {}).get("value")
    if lo is not None and hi is not None and not (0 <= lo <= hi <= 100):
        attributes["age_min"]["value"] = None
        attributes["age_max"]["value"] = None
    att = attributes.get("expected_attendance", {})
    if att.get("value") is not None and att["value"] < 1:
        att["value"] = None
    attributes["tags"] = tag_store.clean_estimates(attributes.get("tags", []))
    for tag in attributes["tags"]:
        tag["confidence"] = min(tag["confidence"], CONFIDENCE_CAP)
    language = attributes.get("language", {})
    if language.get("value") not in {"de", "en", "other", None}:
        language["value"] = None
    venue = attributes.get("venue", {})
    if venue.get("value") is not None:
        venue["value"] = str(venue["value"]).strip()[:120] or None
        if venue.get("evidence") is None:
            venue["value"] = None
    price = attributes.get("stated_price", {})
    lo, hi = price.get("min"), price.get("max")
    if (
        price.get("evidence") is None
        or price.get("currency") not in {"EUR", None}
        or (lo is not None and (lo < 0 or lo > 5000))
        or (hi is not None and (hi < 0 or hi > 5000))
        or (lo is not None and hi is not None and lo > hi)
    ):
        price.update(min=None, max=None, currency=None)
    st = attributes.get("start_time", {})
    if st.get("value") is not None and not _TIME_RE.match(str(st["value"])):
        st["value"] = None


def apply_to_event(tx, event_id, attributes: dict) -> None:
    """Write attributes into the typed §2 columns + the inferred jsonb."""
    age_min = attributes.get("age_min", {}).get("value")
    age_max = attributes.get("age_max", {}).get("value")
    age_conf = min(
        attributes.get("age_min", {}).get("confidence", 0),
        attributes.get("age_max", {}).get("confidence", 0),
    )
    current = tx.execute(
        "SELECT venue_id FROM event WHERE id = %s", (event_id,)
    ).fetchone()
    venue_id = current["venue_id"] if current else None
    venue = attributes.get("venue", {})
    if venue_id is None and venue.get("value") and venue.get("evidence"):
        from eventindex.resolve.venues import VenueResolver

        venue_id = VenueResolver(tx).resolve(venue["value"])
    price = attributes.get("stated_price", {})
    language = attributes.get("language", {})
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
            lang = %(language)s,
            venue_id = coalesce(venue_id, %(venue_id)s),
            price_min = coalesce(price_min, %(price_min)s),
            price_max = coalesce(price_max, %(price_max)s),
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
            "language": language.get("value"),
            "venue_id": venue_id,
            "price_min": price.get("min"),
            "price_max": price.get("max") if price.get("max") is not None else price.get("min"),
            "inferred": Jsonb({
                k: attributes[k] for k in
                ("language", "kid_friendly", "newcomer_friendly", "outdoor",
                 "solo_friendly", "interaction_structure", "energy",
                 "sex_service_context", "venue", "stated_price", "start_time")
                if k in attributes
            }),
        },
    )
    tag_store.replace_inferred(tx, event_id, attributes.get("tags", []))
