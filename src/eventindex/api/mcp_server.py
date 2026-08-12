"""Submission-ready, tool-only MCP surface mounted at ``/mcp``.

Every tool is a read-only view over the deterministic Postgres query core.
The public REST API keeps its general query semantics; this adapter adds the
ChatGPT-specific safety, relevance, response-shaping, and standard
``search``/``fetch`` contracts.
"""

import json
import math
import re
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlencode
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

from eventindex.api.confidence import DEFAULT_MIN_CONFIDENCE
from eventindex.api.search import FILTER_DEFAULTS, QueryBody, SearchFilters, VIENNA
from eventindex.resolve.match import _trigrams

BASE_URL = "https://wasup.at"
_LEGACY_URL = "https://wasup.goedly.com"
_HOST = BASE_URL.removeprefix("https://")
_LEGACY_HOST = _LEGACY_URL.removeprefix("https://")

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, openWorldHint=False
)


class _StrictToolInputsFastMCP(FastMCP):
    """Publish and enforce closed top-level argument objects for every tool.

    FastMCP's generated argument models intentionally ignore unknown fields
    for backwards compatibility. That is unsafe for discovery: a client that
    accidentally flattens ``filters`` could otherwise receive a broad query
    while believing its constraints were applied.
    """

    def add_tool(self, fn, name=None, **kwargs) -> None:
        super().add_tool(fn, name=name, **kwargs)
        tool = self._tool_manager.get_tool(name or fn.__name__)
        if tool is not None:
            tool.parameters["additionalProperties"] = False

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        tool = self._tool_manager.get_tool(name)
        if tool is not None:
            allowed = set(tool.parameters.get("properties", {}))
            unknown = sorted(set(arguments) - allowed)
            if unknown:
                raise ToolError(
                    f"Unknown top-level arguments for {name}: {unknown}. "
                    "Follow the published input schema and keep structured "
                    "fields inside their documented object."
                )
        return await super().call_tool(name, arguments)


class MCPQueryBody(QueryBody):
    """Accept the cached pre-2026-07-24 connector wire shape invisibly.

    Current clients see only QueryBody's scoped literal filters and unified
    `tags`. Already-installed clients may still send these two retired names;
    hiding them from JSON Schema prevents new callers from learning them.
    """

    include_terms: SkipJsonSchema[list[str]] = Field(default_factory=list)
    vibe_terms: SkipJsonSchema[list[str]] = Field(default_factory=list)


mcp = _StrictToolInputsFastMCP(
    "Wasup - Linz Event Index",
    instructions=(
        "Find public events in Linz and roughly 25 km around it. TOOL CHOICE: "
        "use search_events for discovery, dates, filters, preferences, prices, "
        "event scale, and comparisons. Put every jointly desired activity, "
        "topic, format, or atmosphere concept in ONE tags list and make ONE "
        "call; never search once per tag. Use name only for literal event-title "
        "words such as name='ball'; organizer, venue, and reporting source "
        "have their own literal filters. Use standard search/fetch only for a known "
        "specific title, venue, or organizer and clients requiring that "
        "document-search protocol. Use get_event only after selecting an event "
        "or when full provenance is needed; search_events already returns the "
        "price and scale needed for comparisons. Use get_calendar_link for a "
        "read-only .ics subscription. Hard fields such as max_price, is_free, "
        "exclusions, and required_attributes exclude unknown values. Soft "
        "fields such as tags, preferred_max_price, and ordinary audience/scale "
        "preferences retain unknowns and rank by stored confidence. "
        "Discovery defaults to effective event confidence >=0.4 after "
        "freshness decay; set filters.min_confidence=0 only when the user "
        "explicitly asks for tentative or unverified hints. Exact entity "
        "search is an index-presence lookup and may return lower-confidence "
        "records labeled tentative; never reinterpret that label, or an "
        "empty result whose diagnostics report tentative matches, as absent "
        "from the index. "
        "For event tags, origins describe the ingestion path: `inferred` "
        "means model-normalized enrichment, not unsupported. Consult "
        "evidence_bases to distinguish title/text support from world-knowledge "
        "estimates. "
        "Do not use Wasup for Vienna, restaurants, private-event creation, "
        "or invitations. Known commercial sex-service contexts are excluded "
        "unless a supported tool receives an explicit true opt-in. Null "
        "means unknown; projected dates and inferred attributes are estimates."
    ),
    website_url=BASE_URL,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        allowed_hosts=[_HOST, _LEGACY_HOST, "localhost:*", "127.0.0.1:*",
                       "testserver"],
        allowed_origins=[BASE_URL, _LEGACY_URL, "http://localhost:*",
                         "http://127.0.0.1:*"],
    ),
)


class _Output(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Estimate(_Output):
    value: bool | float | str | None
    confidence: float | None


class PriceInfo(_Output):
    min: float | None
    max: float | None
    currency: str
    confidence: float | None
    basis: Literal["stated", "estimated", "unknown"]
    source_url: str | None


class EventScale(_Output):
    estimate_status: Literal["estimated", "unknown"]
    estimated_participants: int | None
    plausible_min: int | None
    plausible_max: int | None
    band: Literal[
        "intimate", "small", "medium", "large", "very_large", "mass"
    ] | None
    confidence: float | None
    basis: list[str]


class TagSupport(_Output):
    score: float
    event_tag: str
    tag_confidence: float
    relatedness: float
    origin: Literal["title", "event_tag"]


class TagConceptMatch(_Output):
    query: str
    score: float
    event_tag: str | None
    tag_confidence: float | None
    relatedness: float
    origin: Literal["title", "event_tag"] | None
    supports: list[TagSupport] = Field(default_factory=list)
    joint: bool = Field(
        False,
        description="True only for a combined-phrase context diagnostic; "
        "this row is never the final multi-concept score.",
    )
    role: Literal["requested_concept", "combined_phrase_context"]


class EventTag(_Output):
    name: str
    confidence: float
    origins: list[str] = Field(
        description="Ingestion provenance. `inferred` means model-enriched or "
        "normalized and does not by itself mean unsupported."
    )
    evidence_bases: list[Literal[
        "source", "category", "title", "explicit_text", "world_knowledge",
        "unknown",
    ]] = Field(
        description="Safe support classification; raw source quotes are never "
        "published."
    )


class EventEstimates(_Output):
    age_min: Estimate | None = None
    age_max: Estimate | None = None
    gender_split: Estimate | None = None
    language: Estimate | None = None
    kid_friendly: Estimate | None = None
    newcomer_friendly: Estimate | None = None
    outdoor: Estimate | None = None
    solo_friendly: Estimate | None = None
    interaction_structure: Estimate | None = None
    energy: Estimate | None = None
    sex_service_context: Estimate | None = None
    venue: Estimate | None = None
    start_time: Estimate | None = None


class SearchOccurrence(_Output):
    id: UUID
    event_id: UUID
    title: str
    url: str
    starts_at: datetime
    ends_at: datetime | None
    ongoing: bool
    projected: bool
    time_unknown: bool
    start_time_estimate: Estimate | None
    category: list[str]
    venue_name: str | None
    venue_address: str | None
    organizer: str | None
    price: PriceInfo
    event_scale: EventScale
    booking_url: str | None
    registration_required: bool | None
    kind: str
    event_status: str
    confidence: float
    match_score: float
    tag_match: float | None = Field(
        description="Final semantic score. Multiple concepts use harmonic "
        "coverage with at most a 10% downward context adjustment."
    )
    tag_weakest_concept_match: float | None = Field(
        description="Score of the weakest individually requested concept."
    )
    tag_weakest_concept: str | None
    tag_context_match: float | None = Field(
        description="Combined-phrase context diagnostic. It never raises "
        "tag_match and is not itself the final score."
    )
    tag_matches: list[TagConceptMatch]
    provenance_summary: list[str]


class SearchDiagnostics(_Output):
    message: str
    suggested_retry: str | None = None


class SearchEventsResponse(_Output):
    data_freshness: datetime | None
    parsed_filters: SearchFilters
    importance: dict[str, float]
    occurrences: list[SearchOccurrence]
    diagnostics: SearchDiagnostics | None = None


class EventOccurrence(_Output):
    id: UUID
    starts_at: datetime
    ends_at: datetime | None
    status: str
    projected: bool
    availability: str | None
    waitlist_url: str | None
    fullness_estimate: float | None
    last_confirmed_at: datetime | None
    time_unknown: bool


class EventSource(_Output):
    name: str
    url: str
    extracted_at: datetime


class EventRecord(_Output):
    id: UUID
    canonical_url: str
    kind: str
    title: str
    description: str | None
    category: list[str]
    tags: list[EventTag]
    venue_name: str | None
    venue_address: str | None
    lat: float | None
    lon: float | None
    organizer: str | None
    registration_required: bool | None
    registration_deadline: datetime | None
    booking_url: str | None
    price: PriceInfo
    event_scale: EventScale
    url: str | None
    image_url: str | None
    language: str | None
    expected_age_range: str | None
    expected_age_range_confidence: float | None
    confidence: float
    status: str
    estimates: EventEstimates
    provenance_summary: list[str]
    updated_at: datetime


class EventDetailResponse(_Output):
    data_freshness: datetime | None
    event: EventRecord
    occurrences: list[EventOccurrence]
    sources: list[EventSource]


class CalendarCoverage(_Output):
    occurrence_id: UUID
    title: str | None
    included: bool
    reasons: list[Literal[
        "not_found", "date_window", "not_scheduled", "geography", "category",
        "confidence", "name", "organizer", "venue", "source",
        "sex_service_safety", "weekday", "time_unknown", "stated_price",
        "event_scale", "tag_match", "tag_concept_match", "feed_limit",
    ]]


class CalendarLinkResponse(_Output):
    ics_url: str | None
    coverage_complete: bool | None = Field(
        description="True/false only when accepted occurrence IDs were "
        "checked; null means no coverage audit was requested."
    )
    coverage: list[CalendarCoverage] = Field(default_factory=list)


class SearchResultStub(_Output):
    id: str
    title: str
    url: str


class StandardSearchResponse(_Output):
    results: list[SearchResultStub]
    hint: str | None = None


class FetchMetadata(_Output):
    confidence: float
    status: str


class StandardFetchResponse(_Output):
    id: str
    title: str
    text: str
    url: str
    metadata: FetchMetadata


def _event_url(event_id: str | UUID) -> str:
    return f"{BASE_URL}/v1/events/{event_id}"


def _estimate(value: Any) -> Estimate | None:
    if not isinstance(value, dict):
        return None
    return Estimate(value=value.get("value"), confidence=value.get("confidence"))


def _estimates(values: dict | None, *, include_sex: bool) -> EventEstimates:
    values = values or {}
    return EventEstimates(
        age_min=_estimate(values.get("age_min")),
        age_max=_estimate(values.get("age_max")),
        gender_split=_estimate(values.get("gender_split")),
        language=_estimate(values.get("language")),
        kid_friendly=_estimate(values.get("kid_friendly")),
        newcomer_friendly=_estimate(values.get("newcomer_friendly")),
        outdoor=_estimate(values.get("outdoor")),
        solo_friendly=_estimate(values.get("solo_friendly")),
        interaction_structure=_estimate(values.get("interaction_structure")),
        energy=_estimate(values.get("energy")),
        sex_service_context=(
            _estimate(values.get("sex_service_context")) if include_sex else None
        ),
        venue=_estimate(values.get("venue")),
        start_time=_estimate(values.get("start_time")),
    )


def _search_occurrence(row: dict) -> SearchOccurrence:
    return SearchOccurrence(
        id=row["id"],
        event_id=row["event_id"],
        title=row["title"],
        url=row.get("url") or _event_url(row["event_id"]),
        starts_at=row["starts_at"],
        ends_at=row.get("ends_at"),
        ongoing=bool(row.get("ongoing")),
        projected=bool(row.get("projected")),
        time_unknown=bool(row.get("time_unknown")),
        start_time_estimate=_estimate(row.get("start_time_estimate")),
        category=list(row.get("category") or []),
        venue_name=row.get("venue_name"),
        venue_address=row.get("venue_address"),
        organizer=row.get("organizer"),
        price=PriceInfo.model_validate(row["price"]),
        event_scale=EventScale.model_validate(row["event_scale"]),
        booking_url=row.get("booking_url"),
        registration_required=row.get("registration_required"),
        kind=row["kind"],
        event_status=row["event_status"],
        confidence=row["confidence"],
        match_score=row["match_score"],
        tag_match=row.get("tag_match"),
        tag_weakest_concept_match=row.get("tag_weakest_concept_match"),
        tag_weakest_concept=row.get("tag_weakest_concept"),
        tag_context_match=row.get("tag_context_match"),
        tag_matches=[
            TagConceptMatch.model_validate(match)
            for match in row.get("tag_matches", [])
        ],
        provenance_summary=list(row.get("provenance_summary") or []),
    )


def _credible_ongoing(row: dict) -> bool:
    """Defense in depth while pre-fix canonical rows age out/rebuild."""
    if not row.get("ongoing"):
        return True
    ends_at = row.get("ends_at")
    if ends_at is None:
        return False
    if set(row.get("category") or []) & {"art", "culture"}:
        return True
    return ends_at - row["starts_at"] <= timedelta(days=14)


def _event_detail(event_id: str, *, include_sex: bool) -> EventDetailResponse:
    from eventindex.api import app as api

    detail = api._event_detail(UUID(event_id), include_policy_marker=True)
    if detail.pop("_sex_service_context") and not include_sex:
        raise ValueError("event unavailable through this tool")
    event = detail["event"]
    return EventDetailResponse(
        data_freshness=detail["data_freshness"],
        event=EventRecord(
            id=event["id"],
            canonical_url=_event_url(event["id"]),
            kind=event["kind"],
            title=event["title"],
            description=event.get("description"),
            category=list(event.get("category") or []),
            tags=list(event.get("tags") or []),
            venue_name=event.get("venue_name"),
            venue_address=event.get("venue_address"),
            lat=event.get("lat"),
            lon=event.get("lon"),
            organizer=event.get("organizer"),
            registration_required=event.get("registration_required"),
            registration_deadline=event.get("registration_deadline"),
            booking_url=event.get("booking_url"),
            price=PriceInfo.model_validate(event["price"]),
            event_scale=EventScale.model_validate(event["event_scale"]),
            url=event.get("url"),
            image_url=event.get("image_url"),
            language=event.get("lang"),
            expected_age_range=event.get("expected_age_range"),
            expected_age_range_confidence=(
                event.get("expected_age_range_confidence")
            ),
            confidence=event["confidence"],
            status=event["status"],
            estimates=_estimates(event.get("estimates"), include_sex=include_sex),
            provenance_summary=list(event.get("provenance_summary") or []),
            updated_at=event["updated_at"],
        ),
        occurrences=[EventOccurrence.model_validate(o) for o in detail["occurrences"]],
        sources=[EventSource.model_validate(s) for s in detail["sources"]],
    )


def _validated_filters(
    body: QueryBody | MCPQueryBody,
) -> tuple[SearchFilters, dict[str, float]]:
    from eventindex.api.search import SOFT_ATTRIBUTES

    values = body.model_dump()
    importance = values.pop("importance")
    include_terms = values.pop("include_terms", [])
    retired_vibe_terms = values.pop("vibe_terms", [])
    if retired_vibe_terms and not values["tags"]:
        # One tag collection: this is only a retired input name, never a
        # second tag type or storage path.
        values["tags"] = retired_vibe_terms
    if include_terms:
        # The retired field was an exact index-presence lookup. Keep it
        # transparent like standard search instead of allowing a stale client
        # to conclude that lower-confidence records do not exist.
        values["min_confidence"] = 0.0
    if not all(
        name in SOFT_ATTRIBUTES and 0 <= weight <= 1
        for name, weight in importance.items()
    ):
        raise ValueError("importance must use known attributes with weights 0..1")
    parsed = SearchFilters(**values)
    parsed._literal_any_terms = [
        cleaned
        for term in include_terms
        if (cleaned := " ".join(term.split())[:120])
    ]
    return parsed, importance


@mcp.tool(title="Search Linz events", annotations=_READ_ONLY)
def search_events(
    filters: MCPQueryBody | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    sort: Literal["relevance", "starts_at"] = "relevance",
) -> SearchEventsResponse:
    """Use this when the user wants to discover or compare public events in
    Linz or roughly 25 km around it by event name, date, category, semantic
    concepts, price, scale, exclusions, or audience preferences.

    Make one call for the whole request. Put a literal event-title word in
    `name`; put literal organizer, venue, and reporting-source names in their
    own filters; put every jointly desired activity/topic/format/atmosphere concept in the
    single `tags` list. Tags are soft by default and jointly scored. With two
    or three concepts the response also shows a `joint=true`,
    role=combined_phrase_context diagnostic: it disambiguates phrases such as
    salsa+dance from salsa food, but is NOT the final score and can only reduce
    final tag_match by at most 10%. tag_weakest_concept_match exposes the
    weakest requested concept. Set `min_tag_match` only for an explicit
    must/only requirement; add `min_tag_concept_match` when every requested
    concept needs its own hard floor.

    Hard versus soft examples:
    - "balls, ideally dancing and elegant":
      filters={"name":"ball","tags":["dance","elegant"],
               "importance":{"tags":1.0}}
    - "WKO startup events":
      filters={"source":"WKO","tags":["startup"]}
    - "ideally under EUR 30": filters={"preferred_max_price":30}
    - "must cost at most EUR 30": filters={"max_price":30}
    - "large, preferably 300+ people":
      filters={"participant_count_min":300}
    - "300+ people, but only reasonably supported estimates":
      filters={"participant_count_min":300,"min_scale_confidence":0.5}
    - "must have 300+ people":
      filters={"participant_count_min":300,
               "required_attributes":["event_scale"]}
    - "include tentative hints too":
      filters={"min_confidence":0,"tags":["experimental music"]}

    Do not put `dance` in name, do not call once for dance and again for
    elegant, and do not call get_event repeatedly merely to compare prices or
    scale: every result already returns those fields, their confidence/basis,
    an explicit `estimate_status`, and per-requested-tag match evidence. When
    tags and secondary preferences
    are combined, semantic fit leads by default; use `importance` only when
    the user's wording clearly assigns a different relative priority.

    Returns one row per event with its next relevant occurrence. Omitted or
    false sex_service_context excludes known commercial sex-service contexts;
    only explicit true permits them. Unknown soft attributes remain visible.
    Empty results include a safe retry hint and never weaken hard requirements."""
    from eventindex.api import app as api

    parsed, importance = _validated_filters(filters or QueryBody())
    exclude_sex = parsed.sex_service_context is not True
    payload = api._run_filters(
        parsed,
        limit=limit,
        importance=importance,
        sort=sort,
        distinct=True,
        exclude_sex_service_context=exclude_sex,
    )
    # Stable partition preserves relevance or chronology inside each group.
    # Actual starts in the window must not be buried under old ongoing spans.
    rows = [row for row in payload["occurrences"] if _credible_ongoing(row)]
    rows = sorted(rows, key=lambda row: bool(row["ongoing"]))
    selected = rows[:limit]
    diagnostics = None
    if not selected:
        tentative = []
        if parsed.min_confidence > 0:
            tentative_filters = parsed.model_copy(
                update={"min_confidence": 0.0}
            )
            tentative_payload = api._run_filters(
                tentative_filters,
                limit=limit,
                importance=importance,
                sort=sort,
                distinct=True,
                exclude_sex_service_context=exclude_sex,
            )
            tentative = [
                row for row in tentative_payload["occurrences"]
                if _credible_ongoing(row)
            ]
        if tentative:
            noun = "match exists" if len(tentative) == 1 else "matches exist"
            diagnostics = SearchDiagnostics(
                message=(
                    f"No event meets min_confidence={parsed.min_confidence:g}, "
                    f"but {len(tentative)} lower-confidence {noun} in the "
                    "index."
                ),
                suggested_retry=(
                    "Retry the same filters with min_confidence=0 to inspect "
                    "the tentative records; present their confidence and do "
                    "not describe them as absent from the index."
                ),
            )
        elif parsed.max_price is not None or parsed.is_free:
            diagnostics = SearchDiagnostics(
                message="No event matched every hard filter; unknown or "
                "estimated prices cannot satisfy max_price/is_free.",
                suggested_retry=(
                    "If price was only a preference, remove max_price/is_free "
                    "and use preferred_max_price."
                ),
            )
        elif (
            parsed.min_tag_match is not None
            or parsed.min_tag_concept_match is not None
        ):
            diagnostics = SearchDiagnostics(
                message="No event reached every required semantic-tag threshold.",
                suggested_retry=(
                    "If the concepts were preferences, omit min_tag_match and "
                    "min_tag_concept_match, and keep all concepts together in tags."
                ),
            )
        else:
            diagnostics = SearchDiagnostics(
                message="No event matched every hard filter.",
                suggested_retry=(
                    "Check the date window and literal name; keep conceptual "
                    "terms in tags rather than name."
                ),
            )
    return SearchEventsResponse(
        data_freshness=payload["data_freshness"],
        parsed_filters=parsed,
        importance=importance,
        occurrences=[_search_occurrence(row) for row in selected],
        diagnostics=diagnostics,
    )


@mcp.tool(title="Get event details", annotations=_READ_ONLY)
def get_event(
    event_id: str,
    include_sex_service_context: bool = False,
) -> EventDetailResponse:
    """Use this when the user selected a Wasup event and wants its sanitized
    public details, future and historical occurrences, confidence estimates,
    source provenance, and safe tag evidence_bases. An inferred tag can still
    be explicitly supported; raw source claims/quotes and suppressed private-location
    evidence are never returned. A known commercial sex-service event is
    unavailable unless include_sex_service_context is explicitly true."""
    return _event_detail(event_id, include_sex=include_sex_service_context)


@mcp.tool(title="Get calendar subscription link", annotations=_READ_ONLY)
def get_calendar_link(
    filters: QueryBody | None = None,
    include_time_unknown: Annotated[
        bool,
        Field(
            description="Include date-only events whose actual start time is "
            "unknown. Keep false unless the user explicitly asks for them."
        ),
    ] = False,
    accepted_occurrence_ids: Annotated[
        list[UUID] | None,
        Field(
            max_length=100,
            description="Occurrence IDs from the accepted search_events rows. "
            "Pass these when converting recommendations into a feed so Wasup "
            "can prove that none are silently omitted.",
        ),
    ] = None,
) -> CalendarLinkResponse:
    """Use this when the user wants a read-only .ics subscription URL for
    public Linz-area events. This only builds a link; it does not subscribe,
    create entries, or invite anyone. Pass the same `filters` object used by
    search_events. A feed needs membership rules rather than ranking: tags
    require an explicit min_tag_match, max_price/is_free use stated prices, and
    participant_count_min/max require
    required_attributes=["event_scale"]. Ranking-only preferred_max_price,
    audience preferences, and importance weights are rejected with a
    correction message.

    When converting accepted search results into a subscription, pass every
    accepted search row's `id` in accepted_occurrence_ids and set
    min_tag_match at or below the weakest accepted result's tag_match. When
    every requested concept must remain represented, also set
    min_tag_concept_match at or below the weakest accepted result's
    tag_weakest_concept_match. Do not
    assume 0.5: for example, a WKO/startup search may warrant
    filters={"source":"WKO","tags":["startup"],"min_tag_match":0.2}.
    If an accepted search row has time_unknown=true, either omit that row from
    the accepted set or pass include_time_unknown=true; the default timed-only
    feed intentionally cannot preserve it. With no accepted IDs,
    coverage_complete is null (not checked), not true.
    Example: filters={"name":"ball","tags":["dance","elegant"],
    "min_tag_match":0.5}. Example large-event feed:
    filters={"categories":["music"],"participant_count_min":300,
    "min_scale_confidence":0.5,"required_attributes":["event_scale"]}.
    Named-day subscriptions use the same hard weekday filter, for example
    filters={"tags":["salsa dancing"],"weekdays":["thursday","friday"],
    "min_tag_match":0.4}.

    The feed defaults to timed events only and always excludes known
    commercial sex-service contexts while retaining unknown classifications.
    Like search, it defaults to effective event confidence >=0.4; put
    min_confidence=0 inside filters only for an explicitly tentative feed."""
    parsed, importance = _validated_filters(filters or QueryBody())
    if not (
        parsed.name or parsed.organizer or parsed.venue or parsed.source
        or parsed.categories or parsed.tags
    ):
        raise ValueError(
            "A calendar subscription requires name, organizer, venue, source, "
            "categories, or tags so it does not silently create a broad "
            "truncated feed."
        )
    if parsed.tags and parsed.min_tag_match is None:
        raise ValueError(
            "Calendar tag filters require an explicit min_tag_match membership "
            "threshold. When preserving accepted search results, set it at or "
            "below the weakest accepted result's tag_match; do not assume 0.5."
        )
    if parsed.age_min is not None or parsed.age_max is not None:
        raise ValueError(
            "Calendar links do not support age ranking/filtering; remove "
            "age_min/age_max instead of silently changing membership."
        )
    if parsed.sex_service_context is True:
        raise ValueError(
            "Generated calendar links always exclude commercial sex-service "
            "contexts and cannot preserve sex_service_context=true searches."
        )
    soft_fields = [
        name for name in (
            "kid_friendly", "newcomer_friendly", "outdoor", "solo_friendly",
            "interaction_structure", "energy", "language",
            "gender_split_min",
        )
        if getattr(parsed, name) is not None
        and name not in parsed.required_attributes
    ]
    if parsed.preferred_max_price is not None or importance or soft_fields:
        raise ValueError(
            "Calendar feeds cannot rank preferences. Remove importance and "
            "preferred_max_price; use max_price for a hard stated-price limit. "
            "Put any supported hard audience/scale constraint in "
            "required_attributes."
        )
    has_scale = (
        parsed.participant_count_min is not None
        or parsed.participant_count_max is not None
    )
    if has_scale and "event_scale" not in parsed.required_attributes:
        raise ValueError(
            "Calendar event scale defines membership: add 'event_scale' to "
            "required_attributes or remove participant_count_min/max."
        )
    unsupported_required = set(parsed.required_attributes) - {"event_scale"}
    if unsupported_required:
        raise ValueError(
            "Calendar links currently support event_scale as a required "
            f"estimated attribute; unsupported: {sorted(unsupported_required)}"
        )
    if parsed.exclude_categories or parsed.exclude_terms:
        raise ValueError(
            "Calendar links do not yet serialize exclusions; remove them from "
            "this subscription filter."
        )
    params = {
        key: value
        for key, value in [
            ("category", ",".join(parsed.categories or [])),
            ("name", parsed.name),
            ("organizer", parsed.organizer),
            ("venue", parsed.venue),
            ("source", parsed.source),
            ("near", parsed.near),
            ("radius", parsed.radius),
            ("weekdays", ",".join(parsed.weekdays)),
            ("from", parsed.from_dt),
            ("to", parsed.to_dt),
            ("min_confidence", parsed.min_confidence),
            ("max_price", parsed.max_price),
            ("is_free", str(parsed.is_free).lower() if parsed.is_free else None),
            ("participant_count_min", parsed.participant_count_min),
            ("participant_count_max", parsed.participant_count_max),
            ("min_scale_confidence", parsed.min_scale_confidence),
    ]
        if value not in (None, "")
    }
    if parsed.tags:
        params["tags"] = ",".join(parsed.tags)
        params["min_tag_match"] = parsed.min_tag_match
        if parsed.min_tag_concept_match is not None:
            params["min_tag_concept_match"] = parsed.min_tag_concept_match
    params["exclude_sex_service_context"] = "true"
    params["include_time_unknown"] = (
        "true" if include_time_unknown else "false"
    )
    params["limit"] = 1000
    ics_url = f"{BASE_URL}/v1/feed.ics?{urlencode(params)}"
    coverage_requested = accepted_occurrence_ids is not None
    accepted_ids = list(dict.fromkeys(accepted_occurrence_ids or []))
    coverage = []
    if accepted_ids:
        from eventindex.api import app as api

        coverage = api._feed_coverage(
            accepted_ids,
            from_=(
                datetime.fromisoformat(parsed.from_dt)
                if parsed.from_dt else None
            ),
            to=datetime.fromisoformat(parsed.to_dt) if parsed.to_dt else None,
            near=parsed.near,
            radius=parsed.radius or "default",
            category=",".join(parsed.categories or []) or None,
            min_confidence=parsed.min_confidence,
            name=parsed.name,
            organizer=parsed.organizer,
            venue=parsed.venue,
            source=parsed.source,
            weekdays=",".join(parsed.weekdays) or None,
            max_price=parsed.max_price,
            is_free=parsed.is_free,
            participant_count_min=parsed.participant_count_min,
            participant_count_max=parsed.participant_count_max,
            min_scale_confidence=parsed.min_scale_confidence,
            tags=",".join(parsed.tags) or None,
            min_tag_match=parsed.min_tag_match or 0,
            min_tag_concept_match=parsed.min_tag_concept_match,
            exclude_sex_service_context=True,
            include_time_unknown=include_time_unknown,
            limit=1000,
        )
    coverage_complete = (
        all(item["included"] for item in coverage)
        if coverage_requested else None
    )
    return CalendarLinkResponse(
        ics_url=ics_url if coverage_complete is not False else None,
        coverage_complete=coverage_complete,
        coverage=[CalendarCoverage.model_validate(item) for item in coverage],
    )


_MIN_BEST_SIM = 0.45       # what counts as a real lexical hit
_TOP_EVIDENCE_SHARE = 0.6  # a qualifying hit must carry near-top query weight
_KEEP_SHARE = 0.5          # keep rows within half of the best row's score

_SEARCH_HINT = (
    "No lexical match. This tool only matches words against event titles, "
    "venues, and organizers. Translate the request into structured filters "
    "and call search_events instead, e.g. filters={\"from_dt\": \"<ISO "
    "datetime>\", \"to_dt\": \"<ISO datetime>\", \"tags\": [\"concert\"]}."
)
_TENTATIVE_SEARCH_HINT = (
    "Lower-confidence exact-name matches are included and explicitly labeled "
    "tentative. Treat them as records present in the index, not as confirmed "
    "recommendations; use fetch for their current confidence and provenance."
)


def _haystack_words(row: dict) -> list[str]:
    text = " ".join(filter(None, [
        row.get("title"), row.get("venue_name"), row.get("venue_address"),
        row.get("organizer"), " ".join(row.get("category") or []),
    ]))
    # Entity names routinely contain meaningful digits (factory300, Ars
    # Electronica 2026). Query lexemes preserve them, so deleting digits only
    # from the indexed side turns exact entities into unrelated fuzzy words.
    return re.findall(r"[^\W_]+", text.lower())


def _token_similarity(token: str, word: str) -> float:
    if token == word:
        return 1.0
    if len(token) >= 4:
        # German compounds are head-final: suffix-anchored containment means
        # a Gartenkonzert(e) IS a konzert, while a Wochentagsmesse is a
        # Messe, not a Woche(nende). <=3 trailing chars covers inflections.
        idx = word.rfind(token)
        if idx >= 0 and len(word) - idx - len(token) <= 3:
            return 0.75
    ta, tb = _trigrams(token), _trigrams(word)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _rank_rows(tokens: list[str], rows: list[dict]) -> list[dict]:
    """Rank rows by IDF-weighted lexical similarity, fail-closed three ways.

    1. Tokens no row matches are dead vocabulary ("wochenend" on a corpus
       without weekend titles): dropped from scoring rather than allowed to
       veto. But when MOST tokens are dead the corpus lacks the query's
       subject - possibly because policy filtering hid the only real match -
       and the surviving incidental token must not resurrect filler.
    2. A row qualifies only through a hit on a token carrying near-top
       rarity weight: "linz" sits in a fifth of all addresses, so it ranks
       but never qualifies a row on its own (real-data spot check: church
       services at "OK Linz" topped a concert query purely on venue hits).
    3. Relevance is relative: rows below half the best row's score are
       dropped instead of backfilling behind a strong match. An unmatched
       rare token dilutes every row equally, so no absolute floor is safe.
    """
    if not tokens:
        return []
    sims_by_row = [
        [max((_token_similarity(t, w) for w in _haystack_words(row)),
             default=0.0)
         for t in tokens]
        for row in rows
    ]
    pool = len(rows)
    df = [sum(sims[i] >= _MIN_BEST_SIM for sims in sims_by_row)
          for i in range(len(tokens))]
    live = [i for i in range(len(tokens)) if df[i] > 0]
    if len(live) * 2 < len(tokens):
        return []
    weight = {i: math.log((pool + 1) / (df[i] + 1)) + 1 for i in live}
    min_gate_weight = _TOP_EVIDENCE_SHARE * max(weight.values())
    exact_gate = {
        i for i in live
        if weight[i] >= min_gate_weight
        and any(sims[i] == 1.0 for sims in sims_by_row)
    }
    total = sum(weight.values())
    scored = []
    for row, sims in zip(rows, sims_by_row):
        # Exact evidence for a rare query token dominates fuzzy containment.
        # This keeps "factory300" on that entity instead of "Data Factory",
        # while typo tolerance remains available when no exact row exists.
        if exact_gate:
            qualifies = any(sims[i] == 1.0 for i in exact_gate)
        else:
            qualifies = any(
                sims[i] >= _MIN_BEST_SIM and weight[i] >= min_gate_weight
                for i in live
            )
        if not qualifies:
            continue
        score = sum(weight[i] * sims[i] for i in live) / total
        scored.append((score, row))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    scored = [(s, r) for s, r in scored if s >= _KEEP_SHARE * best]
    scored.sort(key=lambda pair: (-pair[0], pair[1]["starts_at"]))
    return [row for _, row in scored]


def _stemmed_tokens(query: str) -> list[str]:
    """Normalize the query with Postgres's German snowball dictionary:
    stems plurals/inflections and drops German stopwords - the standard
    tool instead of a hand-rolled vocabulary. Lexeme order is irrelevant
    to the mean score in _rank_rows."""
    from eventindex import db

    with db.connect() as conn:
        row = conn.execute(
            "SELECT tsvector_to_array(to_tsvector('german', %(q)s)) AS lex",
            {"q": query[:200]},
        ).fetchone()
    return row["lex"]


@mcp.tool(title="Search public Linz events by keyword", annotations=_READ_ONLY)
def search(query: str) -> StandardSearchResponse:
    """Use this when the user gives an exact event title, venue, or
    organizer name (fuzzy lexical lookup), or a client requires the
    standard search/fetch contract. You are the translation engine:
    convert natural-language requests into structured filters instead
    of forwarding them here.
    BAD:  search(query="Konzerte am Wochenende in Linz")
    GOOD: search_events(filters={"from_dt": ..., "to_dt": ...,
          "tags": ["concert"]}) for dates, concepts, prices, or scale.
    GOOD: search(query="Posthof") - name lookup is what this tool is for.
    Exact entity lookup also inspects records below the ordinary discovery
    confidence floor, labels them tentative, and returns their current
    confidence. This prevents a temporarily stale source from being mistaken
    for an entity absent from the index. Do not use for other cities,
    restaurants, private events, or invitations. Known commercial sex-service
    events and past-start occurrences are always excluded; fewer than ten
    results is preferable to irrelevant filler."""
    from eventindex.api import app as api

    cutoff = datetime.now(VIENNA)
    tokens = _stemmed_tokens(query)
    if not tokens:
        return StandardSearchResponse(results=[], hint=_SEARCH_HINT)
    filters = SearchFilters(**(
        FILTER_DEFAULTS
        | {"from_dt": cutoff.isoformat(), "min_confidence": 0.0}
    ))
    payload = api._run_filters(
        filters,
        # Standard connector search applies lexical title/venue/organizer
        # relevance after the shared candidate query. Linz-scale future canon
        # is small enough to rank exhaustively; a date-ordered cap here made
        # known far-future events unfindable.
        limit=100_000,
        sort="starts_at",
        distinct=True,
        exclude_sex_service_context=True,
    )
    rows = [row for row in payload["occurrences"] if row["starts_at"] >= cutoff]
    results, seen = [], set()
    has_tentative = False
    for row in _rank_rows(tokens, rows):
        semantic_key = (
            re.sub(r"\W+", "", row["title"].casefold()),
            (row.get("venue_name") or "").casefold(),
        )
        if semantic_key in seen:
            continue
        seen.add(semantic_key)
        local = row["starts_at"].astimezone(VIENNA)
        when = f"{local:%a %Y-%m-%d}"
        when += " (time unknown)" if row["time_unknown"] else f" {local:%H:%M}"
        venue = f" @ {row['venue_name']}" if row.get("venue_name") else ""
        confidence = float(row["confidence"])
        tentative = confidence < DEFAULT_MIN_CONFIDENCE
        has_tentative = has_tentative or tentative
        confidence_label = (
            f"; tentative confidence {confidence:.2f}" if tentative else ""
        )
        results.append(SearchResultStub(
            id=str(row["event_id"]),
            title=f"{row['title']} ({when}{venue}{confidence_label})",
            url=_event_url(row["event_id"]),
        ))
        if len(results) >= 10:
            break
    return StandardSearchResponse(
        results=results,
        hint=(
            _TENTATIVE_SEARCH_HINT if has_tentative
            else None if results
            else _SEARCH_HINT
        ),
    )


def _format_estimates(estimates: EventEstimates) -> str | None:
    values = estimates.model_dump(exclude_none=True)
    if not values:
        return None
    return json.dumps(values, ensure_ascii=False, separators=(",", ":"))


@mcp.tool(title="Fetch public event document", annotations=_READ_ONLY)
def fetch(id: str) -> StandardFetchResponse:
    """Use this when a client needs the full document for an id returned by
    the standard search tool. It returns sanitized public data and current or
    future occurrences only. Raw claims, suppressed private-location evidence,
    and known commercial sex-service events are never returned."""
    detail = _event_detail(id, include_sex=False)
    event = detail.event
    now = datetime.now(VIENNA)
    occurrences = [
        occurrence for occurrence in detail.occurrences
        if (occurrence.ends_at or occurrence.starts_at) >= now
    ]
    dates = []
    for occurrence in occurrences:
        local = occurrence.starts_at.astimezone(VIENNA)
        label = f"{local:%a %Y-%m-%d %H:%M}"
        if occurrence.starts_at < now:
            label += " (ongoing)"
        if occurrence.projected:
            label += " (projected, unconfirmed)"
        if occurrence.time_unknown:
            label += " (time unknown)"
        dates.append(label)
    lines = [
        f"# {event.title}",
        f"Categories: {', '.join(event.category) or 'unknown'}",
        f"Venue: {event.venue_name or 'unknown'}",
        f"Organizer: {event.organizer or 'unknown'}",
        (
            f"Price: {event.price.min}-{event.price.max} EUR "
            f"({event.price.basis}, confidence={event.price.confidence})"
            if event.price.min is not None else "Price: unknown"
        ),
        (
            "Event scale: "
            f"{event.event_scale.estimated_participants} participants "
            f"({event.event_scale.band}, "
            f"confidence={event.event_scale.confidence})"
            if event.event_scale.estimated_participants is not None
            else "Event scale: unknown"
        ),
        "Dates: " + ("; ".join(dates) if dates else "no current or future occurrence"),
        f"Sources: {', '.join(source.name for source in detail.sources) or 'unknown'}",
    ]
    if event.description:
        lines.append(f"Description: {event.description[:2000]}")
    if estimate_text := _format_estimates(event.estimates):
        lines.append(f"Inferred audience attributes (estimates): {estimate_text}")
    return StandardFetchResponse(
        id=id,
        title=event.title,
        text="\n".join(lines),
        url=event.canonical_url,
        metadata=FetchMetadata(confidence=event.confidence, status=event.status),
    )
