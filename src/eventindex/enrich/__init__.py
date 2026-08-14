"""Inferred-attribute enrichment (§8 / H5): priors with humility.

The category prior is the baseline; the LLM may ADJUST it only when the
event's own text gives explicit evidence ("Seniorencafé", "ab 18",
"Anfänger willkommen"), and must say what the evidence was. Confidence is
capped at 0.8 - these are estimates by construction, served labeled.

Results are cached by content hash: canon rebuilds re-apply the cache for
free; only genuinely new/changed events cost an LLM call.
"""

import hashlib
import json
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


class _AudienceFloatEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: float
    confidence: float = Field(gt=0, le=1)


class _AudienceEnergyEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Literal["low", "medium", "high"]
    confidence: float = Field(gt=0, le=1)


class _AudienceBoolEst(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: bool
    confidence: float = Field(gt=0, le=1)


class AudienceEssentials(BaseModel):
    """The exhaustive audience facets required before publication."""

    model_config = ConfigDict(extra="forbid")
    gender_split: _AudienceFloatEst
    energy: _AudienceEnergyEst
    solo_friendly: _AudienceBoolEst

    @model_validator(mode="after")
    def valid_gender_split(self):
        if not 0 <= self.gender_split.value <= 1:
            raise ValueError("gender_split must be between 0 and 1")
        return self


class _AudienceBatchItem(AudienceEssentials):
    event_id: str


class AudienceEssentialsBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[_AudienceBatchItem] = Field(
        min_length=1,
        max_length=config.AUDIENCE_ESSENTIALS_BATCH_SIZE,
    )


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
        description="6-12 retrieval concepts covering the core activity, "
        "participant actions, format, interaction, distinctive experience, "
        "and explicit secondary activities; no structured-metadata filler, "
        "mechanical parent expansion, synonyms, translations, or commentary")
    venue: _TextEst
    price: _PriceEst
    start_time: _TimeEst

    @model_validator(mode="after")
    def coherent_query_attributes(self):
        if not (0 <= self.age_min.value <= self.age_max.value <= 100):
            raise ValueError("age estimates must satisfy 0 <= min <= max <= 100")
        if not 0 <= self.gender_split.value <= 1:
            raise ValueError("gender_split must be between 0 and 1")
        cleaned_tags = tag_store.clean_estimates(
            tag.model_dump() for tag in self.tags
        )
        if len(cleaned_tags) < MIN_INFERRED_TAGS:
            raise ValueError(
                "tags must contain at least 6 distinct useful concepts after "
                "structured-metadata filler is removed"
            )
        return self


# Bump when the schema or extraction contract changes: old cache rows either
# lack fields or embody the old prompt, so a version change re-enriches them.
SCHEMA_VERSION = 11
AUDIENCE_ESSENTIALS_SCHEMA_VERSION = 2
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


def audience_essentials_content_key(event: dict) -> str:
    """A separate, small-context cache key for publication essentials."""

    parts = "|".join([
        f"audience-essentials-v{AUDIENCE_ESSENTIALS_SCHEMA_VERSION}",
        event.get("title") or "",
        (event.get("description") or "")[
            :config.AUDIENCE_ESSENTIALS_DESCRIPTION_CHARS
        ],
        ",".join(event.get("category") or []),
        str(event.get("venue_name") or ""),
    ])
    return hashlib.md5(parts.encode()).hexdigest()


def _prior_for(tx, categories: list[str]) -> dict:
    if not categories:
        return {}
    row = tx.execute(
        "SELECT priors FROM category_priors WHERE category = %s", (categories[0],)
    ).fetchone()
    return row["priors"] if row else {}


def _clamp_audience_essentials(attributes: dict, _event: dict) -> dict:
    """Cap certainty without ever discarding a required estimate."""

    for name in ("gender_split", "energy", "solo_friendly"):
        estimate = attributes[name]
        # This deliberately tiny pass does not retain verbatim evidence. Keep
        # every result at prior-level certainty; the later full enrichment can
        # carry richer evidence without making publication wait for it.
        estimate["confidence"] = min(
            estimate["confidence"], PRIOR_CONFIDENCE_CAP
        )
    return attributes


def persist_audience_essentials(
    tx, event: dict, attributes: dict, *, model: str
) -> tuple[str, dict]:
    """Commit one validated essentials cache winner for this exact content."""

    key = audience_essentials_content_key(event)
    values = _clamp_audience_essentials(
        AudienceEssentials.model_validate(attributes).model_dump(), event
    )
    persisted = tx.execute(
        "INSERT INTO enrichment (content_key, attributes, model) "
        "VALUES (%s, %s, %s) ON CONFLICT (content_key) DO UPDATE "
        "SET content_key = enrichment.content_key RETURNING attributes",
        (key, Jsonb(values), model),
    ).fetchone()["attributes"]
    return key, _clamp_audience_essentials(
        AudienceEssentials.model_validate(persisted).model_dump(), event
    )


def with_audience_essentials(attributes: dict, essentials: dict) -> dict:
    """Keep the publication facets equal to their committed cache winner."""

    merged = dict(attributes)
    merged["gender_split"] = essentials["gender_split"]
    merged["energy"] = essentials["energy"]["value"]
    merged["solo_friendly"] = essentials["solo_friendly"]
    return merged


def audience_essentials_from_full(attributes: dict) -> dict | None:
    """Read the required facets from the existing full-enrichment contract.

    Full schema v11 stored energy as an enum without a confidence. It is still
    an AI estimate and is safe for exhaustive filtering; expose a deliberately
    low confidence when adapting that legacy cache rather than paying again.
    """

    if not isinstance(attributes, dict):
        return None
    gender = attributes.get("gender_split") or {}
    energy = attributes.get("energy")
    solo_friendly = attributes.get("solo_friendly") or {}
    if isinstance(energy, str):
        energy = {
            "value": energy,
            "confidence": GUESS_CONFIDENCE,
        }
    elif isinstance(energy, dict):
        energy = {
            "value": energy.get("value"),
            "confidence": energy.get("confidence"),
        }
    try:
        return AudienceEssentials.model_validate({
            "gender_split": {
                "value": gender.get("value"),
                "confidence": gender.get("confidence"),
            },
            "energy": energy,
            "solo_friendly": {
                "value": solo_friendly.get("value"),
                "confidence": solo_friendly.get("confidence"),
            },
        }).model_dump()
    except (AttributeError, ValueError, TypeError):
        return None


def estimate_audience_essentials(
    tx, events: list[dict], *, job_id=None
) -> dict[str, tuple[str, dict]]:
    """Return cached/new essentials for at most one cheap LLM batch.

    The mapping key is the canonical event id as text; each value carries its
    versioned cache key and strict attributes. Concurrent workers both return
    the committed cache winner, matching the full-enrichment cache contract.
    """

    if not events:
        return {}
    if len(events) > config.AUDIENCE_ESSENTIALS_BATCH_SIZE:
        raise ValueError(
            "audience essentials batch exceeds "
            f"{config.AUDIENCE_ESSENTIALS_BATCH_SIZE} events"
        )
    event_by_id = {str(event["id"]): event for event in events}
    if len(event_by_id) != len(events):
        raise ValueError("audience essentials batch contains duplicate event ids")
    keys = {
        event_id: audience_essentials_content_key(event)
        for event_id, event in event_by_id.items()
    }
    cached = {
        row["content_key"]: row["attributes"]
        for row in tx.execute(
            "SELECT content_key, attributes FROM enrichment "
            "WHERE content_key = ANY(%s)",
            (list(keys.values()),),
        )
    }
    resolved: dict[str, tuple[str, dict]] = {}
    missing: list[dict] = []
    for event_id, event in event_by_id.items():
        key = keys[event_id]
        try:
            attributes = _clamp_audience_essentials(
                AudienceEssentials.model_validate(cached[key]).model_dump(),
                event,
            )
        except (KeyError, ValueError, TypeError):
            missing.append(event)
        else:
            resolved[event_id] = (key, attributes)

    if not missing:
        return resolved

    prompt_events = [
        {
            "event_id": str(event["id"]),
            "title": (event.get("title") or "")[:300],
            "description": (event.get("description") or "")[
                :config.AUDIENCE_ESSENTIALS_DESCRIPTION_CHARS
            ],
            "category": [
                str(name)[:40]
                for name in (event.get("category") or [])[:3]
            ],
            "venue": str(event.get("venue_name") or "")[:160] or None,
        }
        for event in missing
    ]
    result = llm.complete(
        tx,
        "Estimate exactly three exhaustive audience facets for every event in "
        "the JSON array below. Return each event_id exactly once. ALWAYS make "
        "a best estimate; no value may be null or unknown. gender_split is a "
        "number from 0 (all men) to 1 (all women). energy is exactly low, "
        "medium, or high and describes the attendee experience, not listing "
        "quality. solo_friendly is true when attending alone is normal and "
        "comfortable (for example a concert or run club), false when the "
        "format normally requires an existing partner/group. Confidence "
        "encodes uncertainty: use about 0.2 for a pure world-knowledge guess "
        "and at most 0.35 for a strong category/format/context estimate. "
        "Return only these values and confidence; "
        "do not explain outside the schema.\n\nEVENTS:\n"
        + json.dumps(prompt_events, ensure_ascii=False, separators=(",", ":")),
        AudienceEssentialsBatch,
        job_id=job_id,
        budget_lane="core",
        max_tokens=config.AUDIENCE_ESSENTIALS_MAX_OUTPUT_TOKENS,
        reservation_eur=config.AUDIENCE_ESSENTIALS_RESERVATION_EUR,
        reasoning_effort="none",
    )
    returned = [item.event_id for item in result.events]
    expected = {str(event["id"]) for event in missing}
    if len(returned) != len(set(returned)) or set(returned) != expected:
        raise ValueError(
            "audience essentials response must contain every requested "
            "event_id exactly once"
        )
    result_by_id = {item.event_id: item for item in result.events}
    for event in missing:
        event_id = str(event["id"])
        attributes = _clamp_audience_essentials(
            result_by_id[event_id].model_dump(exclude={"event_id"}), event
        )
        resolved[event_id] = persist_audience_essentials(
            tx, event, attributes, model=config.MODEL_MINI
        )
    return resolved


def apply_audience_essentials(
    tx, event_id, attributes: dict, *, enrichment_key: str
) -> None:
    """Project mandatory audience values while keeping legacy energy shape."""

    values = AudienceEssentials.model_validate(attributes).model_dump()
    for estimate in values.values():
        estimate["confidence"] = min(estimate["confidence"], CONFIDENCE_CAP)
    tx.execute(
        """
        WITH next_values AS (
            SELECT
                %(gender)s::double precision AS gender,
                %(gender_conf)s::double precision AS gender_conf,
                coalesce(e.inferred, '{}'::jsonb)
                || jsonb_build_object('energy', %(energy)s::text)
                || jsonb_build_object(
                    'solo_friendly', %(solo_estimate)s::jsonb
                )
                || jsonb_build_object(
                    '_audience_essentials',
                    jsonb_build_object(
                        'schema_version', %(schema_version)s::int,
                        'content_key', %(content_key)s::text,
                        'gender_split', %(gender_estimate)s::jsonb,
                        'energy', %(energy_estimate)s::jsonb,
                        'solo_friendly', %(solo_estimate)s::jsonb
                    )
                ) AS inferred
            FROM event e
            WHERE e.id = %(id)s
        )
        UPDATE event e SET
            expected_gender_split = next.gender,
            expected_gender_split_confidence = next.gender_conf,
            updated_at = CASE
                WHEN ROW(
                    e.expected_gender_split,
                    e.expected_gender_split_confidence,
                    e.inferred
                ) IS DISTINCT FROM ROW(
                    next.gender,
                    next.gender_conf,
                    next.inferred
                )
                THEN greatest(
                    e.updated_at + interval '1 microsecond',
                    statement_timestamp()
                )
                ELSE e.updated_at
            END,
            inferred = next.inferred
        FROM next_values next
        WHERE e.id = %(id)s
        """,
        {
            "id": event_id,
            "gender": values["gender_split"]["value"],
            "gender_conf": values["gender_split"]["confidence"],
            "energy": values["energy"]["value"],
            "schema_version": AUDIENCE_ESSENTIALS_SCHEMA_VERSION,
            "content_key": enrichment_key,
            "gender_estimate": Jsonb(values["gender_split"]),
            "energy_estimate": Jsonb(values["energy"]),
            "solo_estimate": Jsonb(values["solo_friendly"]),
        },
    )


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
        "tags: propose 8-12 distinct retrieval concepts so at least 6 useful "
        "ones remain after validation. Cover the core activity/topic, event "
        "format, what participants actually do, social/interaction mechanics, "
        "distinctive atmosphere/style or experience, and meaningful secondary "
        "activities. Include an optional or secondary participant action when "
        "the description explicitly offers it; do not omit it merely because "
        "it is not the title's primary noun. Examples of actions include sing, "
        "move or dance, pair up, converse, network, cook, improvise, compete, "
        "and collaborate. Always include the core named activity or event format "
        "when the title states it; explicit title evidence may have confidence "
        "0.8. Each tag is 1-3 lowercase words. "
        "Do not spend tags on information already represented by structured "
        "fields: city/metro area, time of day, price, indoor setting, age or "
        "gender estimates, audience size, language, or generic category. Do "
        "not emit generic tags like 'event' or 'linz', commentary, duplicate "
        "synonyms, or translations of the same concept. "
        "For an important specific 2-3 word activity, include a broader parent "
        "only when it materially broadens a plausible discovery query and is "
        "a genuinely distinct concept: movement to music + movement and mantra "
        "singing + singing can help; night out + night and running club + club "
        "do not. Never add one-word head tags mechanically or to fill quota. "
        "A normalized tag may be English while its evidence remains German or "
        "another source language. Evidence must always be the exact original "
        "source-language quote, never its translation or a paraphrase (for "
        "example tag 'movement' may quote 'Bewegen zu den Klangwelten'). An "
        "explicitly stated or clearly text-entailed activity may use confidence "
        "up to 0.8 with that quote; world knowledge stays low-confidence. "
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
        "SELECT venue_id, inferred FROM event WHERE id = %s", (event_id,)
    ).fetchone()
    venue_id = current["venue_id"] if current else None
    audience_metadata = (
        (current["inferred"] or {}).get("_audience_essentials")
        if current else None
    )
    venue = attributes.get("venue", {})
    if venue_id is None and venue.get("value") and venue.get("evidence"):
        from eventindex.resolve.venues import VenueResolver

        venue_id = VenueResolver(tx).resolve(venue["value"])
    price = attributes.get("price", {})
    scale = attributes.get("event_scale", {})
    language = attributes.get("language", {})
    inferred_values = {
        k: attributes[k] for k in
        ("language", "kid_friendly", "newcomer_friendly", "outdoor",
         "solo_friendly", "interaction_structure", "energy",
         "sex_service_context", "venue", "price", "event_scale",
         "start_time")
        if k in attributes
    }
    if isinstance(inferred_values.get("energy"), dict):
        inferred_values["energy"] = inferred_values["energy"].get("value")
    if audience_metadata:
        inferred_values["_audience_essentials"] = audience_metadata
    tx.execute(
        """
        WITH next_values AS (
            SELECT
            CASE
                WHEN %(age_min)s::int IS NULL OR %(age_max)s::int IS NULL THEN NULL
                ELSE int4range(%(age_min)s, %(age_max)s, '[]')
            END AS age_range,
            %(age_conf)s::double precision AS age_conf,
            %(gender)s::double precision AS gender,
            %(gender_conf)s::double precision AS gender_conf,
            %(attendance)s::int AS attendance,
            %(attendance_conf)s::double precision AS attendance_conf,
            %(language)s::text AS language,
            coalesce(e.venue_id, %(venue_id)s::uuid) AS venue_id,
            coalesce(e.price_min, %(price_min)s::numeric) AS price_min,
            coalesce(e.price_max, %(price_max)s::numeric) AS price_max,
            %(inferred)s::jsonb AS inferred
            FROM event e
            WHERE e.id = %(id)s
        )
        UPDATE event e SET
            expected_age_range = next.age_range,
            expected_age_range_confidence = next.age_conf,
            expected_gender_split = next.gender,
            expected_gender_split_confidence = next.gender_conf,
            expected_attendance = next.attendance,
            expected_attendance_confidence = next.attendance_conf,
            lang = next.language,
            venue_id = next.venue_id,
            price_min = next.price_min,
            price_max = next.price_max,
            updated_at = CASE
                WHEN ROW(
                    e.expected_age_range,
                    e.expected_age_range_confidence,
                    e.expected_gender_split,
                    e.expected_gender_split_confidence,
                    e.expected_attendance,
                    e.expected_attendance_confidence,
                    e.lang,
                    e.venue_id,
                    e.price_min,
                    e.price_max,
                    e.inferred
                ) IS DISTINCT FROM ROW(
                    next.age_range,
                    next.age_conf,
                    next.gender,
                    next.gender_conf,
                    next.attendance,
                    next.attendance_conf,
                    next.language,
                    next.venue_id,
                    next.price_min,
                    next.price_max,
                    next.inferred
                )
                THEN greatest(
                    e.updated_at + interval '1 microsecond',
                    statement_timestamp()
                )
                ELSE e.updated_at
            END,
            inferred = next.inferred
        FROM next_values next
        WHERE e.id = %(id)s
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
                inferred_values | {
                    "_enrichment": {
                        "schema_version": SCHEMA_VERSION,
                        "content_key": enrichment_key,
                    }
                }
            ),
        },
    )
    tag_store.replace_inferred(tx, event_id, attributes.get("tags", []))
