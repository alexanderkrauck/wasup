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
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eventindex import config, llm, tags as tag_store

CONFIDENCE_CAP = 0.8
PRIOR_CONFIDENCE_CAP = 0.35
# Confidence tiers (Alexander 2026-07-06: ALWAYS estimate; confidence says
# how much it's a guess): ~0.2 pure world-knowledge guess, ~0.35 typical for
# this kind of event, up to 0.8 with explicit textual evidence.
GUESS_CONFIDENCE = 0.2


class _Est(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float
    confidence: float = Field(gt=0, le=1)
    evidence: str | None = Field(description="verbatim text snippet, or null if prior only")


class _BoolEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: bool
    confidence: float = Field(gt=0, le=1)
    evidence: str | None


class _TimeEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | None = Field(description="HH:MM, 24h local, or null")
    confidence: float = Field(gt=0, le=1)
    evidence: str | None


class _LanguageEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Literal["de", "en", "other"]
    confidence: float = Field(gt=0, le=1)
    evidence: str | None


class _TextEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str | None
    confidence: float = Field(ge=0, le=1)
    evidence: str | None


class _PriceEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    min: float = Field(ge=0, le=5000)
    max: float = Field(ge=0, le=5000)
    currency: Literal["EUR"]
    basis: Literal["stated", "estimated"]
    confidence: float = Field(gt=0, le=1)
    evidence: str | None

    @model_validator(mode="after")
    def ordered_range(self):
        if self.min > self.max:
            raise ValueError("price min must not exceed max")
        return self


class _TagEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    confidence: float = Field(gt=0, le=1)
    evidence: str | None

    @field_validator("name")
    @classmethod
    def useful_short_name(cls, value: str) -> str:
        clean = tag_store.clean_name(value)
        if clean is None:
            raise ValueError("tag must be a useful 1-3 word concept")
        return clean


class _EventScaleEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    estimated_participants: int = Field(ge=1, le=1_000_000)
    plausible_min: int = Field(ge=1, le=1_000_000)
    plausible_max: int = Field(ge=1, le=1_000_000)
    confidence: float = Field(gt=0, le=1)
    basis: list[str]
    evidence: str | None

    @model_validator(mode="after")
    def ordered_range(self):
        if not (
            self.plausible_min
            <= self.estimated_participants
            <= self.plausible_max
        ):
            raise ValueError("participant estimate must be inside plausible range")
        return self


class Enrichment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # descriptions live in the prompt: strict schema mode forbids
    # annotations on $ref fields
    age_min: _Est
    age_max: _Est
    gender_split: _Est
    event_scale: _EventScaleEst
    language: _LanguageEst
    kid_friendly: _BoolEst
    newcomer_friendly: _BoolEst
    outdoor: _BoolEst
    solo_friendly: _BoolEst
    interaction_structure: Literal["none", "optional", "built_in"]
    energy: Literal["low", "medium", "high"]
    sex_service_context: _BoolEst
    tags: list[_TagEst] = Field(
        min_length=6,
        max_length=12,
        description="6-12 useful event concepts, each 1-3 lowercase words "
        "with its own confidence, including the core named format and useful "
        "atmosphere/style; no synonyms, translations, or commentary")
    venue: _TextEst
    price: _PriceEst
    start_time: _TimeEst

    @model_validator(mode="after")
    def coherent_query_attributes(self):
        if not (0 <= self.age_min.value <= self.age_max.value <= 100):
            raise ValueError("age estimates must satisfy 0 <= min <= max <= 100")
        if not 0 <= self.gender_split.value <= 1:
            raise ValueError("gender_split must be between 0 and 1")
        if len({tag.name for tag in self.tags}) < 6:
            raise ValueError("tags must contain at least 6 distinct concepts")
        return self


# Bump when the schema or extraction contract changes: old cache rows either
# lack fields or embody the old prompt, so a version change re-enriches them.
SCHEMA_VERSION = 10
MIN_INFERRED_TAGS = 6
DESCRIPTION_CHARS = 6000


def content_key(event: dict) -> str:
    def present(value) -> str:
        return "" if value is None else str(value)

    parts = "|".join([
        f"v{SCHEMA_VERSION}",
        event.get("title") or "",
        (event.get("description") or "")[:DESCRIPTION_CHARS],
        ",".join(event.get("category") or []), str(event.get("venue_name") or ""),
        present(event.get("price_min")), present(event.get("price_max")),
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


def exact_price_override(event: dict, attributes: dict) -> dict:
    """A canonical stated price always outranks a fresh model estimate."""
    if event.get("price_min") is not None:
        attributes["price"] = {
            "min": float(event["price_min"]),
            "max": float(
                event["price_max"]
                if event.get("price_max") is not None else event["price_min"]
            ),
            "currency": "EUR",
            "basis": "stated",
            "confidence": CONFIDENCE_CAP,
            "evidence": "canonical stated price",
        }
    return attributes


def _live_overrides(event: dict, attributes: dict) -> dict:
    return exact_price_override(event, venue_override(event, attributes))


def enrich_event(tx, event: dict, job_id=None) -> dict:
    """Compute (or fetch cached) inferred attributes for one canonical event.
    Returns the attributes dict."""
    key = content_key(event)
    cached = tx.execute(
        "SELECT attributes FROM enrichment WHERE content_key = %s", (key,)
    ).fetchone()
    if cached:
        return _live_overrides(event, cached["attributes"])

    prior = _prior_for(tx, event.get("category") or [])
    result = llm.complete(
        tx,
        "Estimate audience attributes for this Linz event. ALWAYS give your "
        "best estimate. Every queryable audience attribute must have a value "
        "and every estimate/tag must have confidence greater than zero; never "
        "use null or zero confidence to mean 'not sure'. "
        "Evidence must be an exact, contiguous substring copied from TITLE, "
        "DESCRIPTION, VENUE, or PRICE, never an explanation or absence-of-"
        "evidence rationale; use null when no such quote exists. "
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
        "event_scale: ALWAYS estimate the number of participants plus a "
        "plausible low/high range. Use explicit attendance/capacity/registration "
        "evidence first, then venue capacity, past editions, organizer draw, "
        "and finally event-format world knowledge. Put short basis labels in "
        "basis; confidence ~0.2 for a pure guess, ~0.35 for a normal type/venue "
        "estimate, and up to 0.8 only for explicit text. "
        "tags: provide 6-12 distinct, useful concepts covering activity/topic, "
        "format, audience, atmosphere/style, and setting. Always include the "
        "core named activity or event format when the title states it; explicit "
        "title evidence may have confidence 0.8. Also include a broader parent "
        "activity when it is useful for retrieval and not merely a synonym. "
        "Each tag is 1-3 lowercase words. "
        "Do not emit generic tags like 'event' or 'linz', commentary, duplicate "
        "synonyms, or translations of the same concept. A tag may use world "
        "knowledge at low confidence; quote evidence when explicit. "
        "venue: only a public venue/organization name explicitly present in "
        "TITLE or DESCRIPTION; never guess and never return an address. "
        "price: ALWAYS return the best EUR admission-price range. If TITLE, "
        "DESCRIPTION, or PRICE explicitly states it, basis='stated' and quote "
        "the exact evidence. Otherwise basis='estimated', infer a plausible "
        "range from event type/venue/past editions, use low confidence, and "
        "leave evidence null. A likely free event is 0-0 with basis estimated "
        "unless free entry is explicit. "
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
        f"DESCRIPTION: {(event.get('description') or '')[:DESCRIPTION_CHARS]}\n"
        f"CATEGORY: {event.get('category')}\nVENUE: {event.get('venue_name')}\n"
        f"PRICE: {event.get('price_min')}-{event.get('price_max')}",
        Enrichment,
        job_id=job_id,
    )
    attributes = result.model_dump()
    for entry in attributes.values():  # the cap is code, not model discipline
        if isinstance(entry, dict) and "confidence" in entry:
            entry["confidence"] = min(entry["confidence"], CONFIDENCE_CAP)
            if entry.get("evidence") is None:
                entry["confidence"] = min(
                    entry["confidence"], PRIOR_CONFIDENCE_CAP
                )
    _sanity_clamp(
        attributes,
        " ".join([
            event.get("title") or "",
            (event.get("description") or "")[:DESCRIPTION_CHARS],
            event.get("venue_name") or "",
            str(event.get("price_min") or ""),
            str(event.get("price_max") or ""),
        ]),
    )
    # Two workers can miss the cache together and produce different valid
    # estimates. Canon must apply the one value that actually won the cache
    # race, otherwise the event and its persisted content cache diverge until
    # another rebuild happens to repair it. The no-op update locks/returns the
    # committed winner without replacing it.
    persisted = tx.execute(
        "INSERT INTO enrichment (content_key, attributes, model) VALUES (%s, %s, %s) "
        "ON CONFLICT (content_key) DO UPDATE "
        "SET content_key = enrichment.content_key "
        "RETURNING attributes",
        (key, Jsonb(attributes), config.MODEL_MINI),
    ).fetchone()
    return _live_overrides(event, persisted["attributes"])


_TIME_RE = __import__("re").compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _sanity_clamp(attributes: dict, source_text: str = "") -> None:
    """Deterministic guards (audit A11: age range [18,7251), attendance 0,
    LLM commentary leaking into tags). Estimates stay estimates, but
    impossible values are rejected rather than silently becoming unknown."""
    lo = attributes.get("age_min", {}).get("value")
    hi = attributes.get("age_max", {}).get("value")
    if lo is None or hi is None or not (0 <= lo <= hi <= 100):
        raise ValueError("invalid age range")
    source_folded = source_text.casefold()
    for entry in attributes.values():
        if not isinstance(entry, dict) or "confidence" not in entry:
            continue
        evidence = str(entry.get("evidence") or "").strip()
        if evidence and evidence.casefold() not in source_folded:
            entry["evidence"] = None
            entry["confidence"] = min(
                entry["confidence"], PRIOR_CONFIDENCE_CAP
            )
    scale = attributes.get("event_scale", {})
    estimate = scale.get("estimated_participants")
    low, high = scale.get("plausible_min"), scale.get("plausible_max")
    if (
        not all(isinstance(v, int) for v in (estimate, low, high))
        or not (1 <= low <= estimate <= high <= 1_000_000)
    ):
        raise ValueError("invalid event_scale range")
    scale["basis"] = [
        str(item).strip()[:80]
        for item in scale.get("basis", [])
        if str(item).strip()
    ][:5] or ["event format"]
    attributes["tags"] = tag_store.clean_estimates(attributes.get("tags", []))
    if len(attributes["tags"]) < MIN_INFERRED_TAGS:
        raise ValueError(
            f"enrichment must contain at least {MIN_INFERRED_TAGS} distinct tags"
        )
    for tag in attributes["tags"]:
        tag["confidence"] = min(tag["confidence"], CONFIDENCE_CAP)
        evidence = str(tag.get("evidence") or "").strip()
        if evidence and evidence.casefold() not in source_folded:
            tag["evidence"] = None
        if tag.get("evidence") is None:
            tag["confidence"] = min(
                tag["confidence"], PRIOR_CONFIDENCE_CAP
            )
    language = attributes.get("language", {})
    if language.get("value") not in {"de", "en", "other", None}:
        language["value"] = None
    venue = attributes.get("venue", {})
    if venue.get("value") is not None:
        venue["value"] = str(venue["value"]).strip()[:120] or None
        if venue.get("evidence") is None:
            venue["value"] = None
    price = attributes.get("price", {})
    lo, hi = price.get("min"), price.get("max")
    if (
        price.get("currency") != "EUR"
        or lo is None or hi is None
        or lo < 0 or lo > 5000
        or hi < 0 or hi > 5000
        or lo > hi
    ):
        raise ValueError("invalid price range")
    evidence = str(price.get("evidence") or "").strip()
    if price.get("basis") == "stated" and (
        not evidence or evidence.casefold() not in source_text.casefold()
    ):
        # A number without quoted source support remains useful as a low-
        # confidence estimate, but must never become an exact price fact.
        price["basis"] = "estimated"
        price["confidence"] = min(price.get("confidence", 0), GUESS_CONFIDENCE)
        price["evidence"] = None
    if price.get("basis") == "estimated":
        price["confidence"] = min(price.get("confidence", 0), 0.35)
    st = attributes.get("start_time", {})
    if st.get("value") is not None and not _TIME_RE.match(str(st["value"])):
        st["value"] = None


def apply_to_event(
    tx, event_id, attributes: dict, *, enrichment_key: str | None = None
) -> None:
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
    price = attributes.get("price", {})
    scale = attributes.get("event_scale", {})
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
            "attendance": scale.get("estimated_participants"),
            "attendance_conf": scale.get("confidence"),
            "language": language.get("value"),
            "venue_id": venue_id,
            "price_min": price.get("min") if price.get("basis") == "stated" else None,
            "price_max": price.get("max") if price.get("basis") == "stated" else None,
            "inferred": Jsonb(
                {
                    k: attributes[k] for k in
                    ("language", "kid_friendly", "newcomer_friendly", "outdoor",
                     "solo_friendly", "interaction_structure", "energy",
                     "sex_service_context", "venue", "price", "event_scale",
                     "start_time")
                    if k in attributes
                } | {
                    "_enrichment": {
                        "schema_version": SCHEMA_VERSION,
                        "content_key": enrichment_key,
                    }
                }
            ),
        },
    )
    tag_store.replace_inferred(tx, event_id, attributes.get("tags", []))
