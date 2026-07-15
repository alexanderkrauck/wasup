"""Submission-ready, tool-only MCP surface mounted at ``/mcp``.

Every tool is a read-only view over the deterministic Postgres query core.
The public REST API keeps its general query semantics; this adapter adds the
ChatGPT-specific safety, relevance, response-shaping, and standard
``search``/``fetch`` contracts.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from urllib.parse import urlencode
from uuid import UUID

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field

from eventindex.api.search import FILTER_DEFAULTS, QueryBody, SearchFilters, VIENNA
from eventindex.resolve.match import _trigrams

BASE_URL = "https://wasup.at"
_LEGACY_URL = "https://wasup.goedly.com"
_HOST = BASE_URL.removeprefix("https://")
_LEGACY_HOST = _LEGACY_URL.removeprefix("https://")

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, openWorldHint=False
)

mcp = FastMCP(
    "Wasup - Linz Event Index",
    instructions=(
        "Find public events in Linz and roughly 25 km around it. Use "
        "search_events for structured date/category/price requests, search "
        "and fetch for keyword retrieval, get_event after selecting a "
        "result, and get_calendar_link for read-only .ics subscriptions. "
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


class EventEstimates(_Output):
    age_min: Estimate | None = None
    age_max: Estimate | None = None
    gender_split: Estimate | None = None
    expected_attendance: Estimate | None = None
    language: Estimate | None = None
    kid_friendly: Estimate | None = None
    newcomer_friendly: Estimate | None = None
    outdoor: Estimate | None = None
    solo_friendly: Estimate | None = None
    interaction_structure: Estimate | None = None
    energy: Estimate | None = None
    sex_service_context: Estimate | None = None
    vibe_tags: list[str] = Field(default_factory=list)
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
    price_min: float | None
    price_max: float | None
    booking_url: str | None
    registration_required: bool | None
    kind: str
    event_status: str
    confidence: float
    match_score: float
    provenance_summary: list[str]


class SearchEventsResponse(_Output):
    data_freshness: datetime | None
    parsed_filters: SearchFilters
    importance: dict[str, float]
    pool_truncated: bool
    occurrences: list[SearchOccurrence]


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
    tags: list[str]
    venue_name: str | None
    venue_address: str | None
    lat: float | None
    lon: float | None
    organizer: str | None
    registration_required: bool | None
    registration_deadline: datetime | None
    booking_url: str | None
    price_min: float | None
    price_max: float | None
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


class CalendarLinkResponse(_Output):
    ics_url: str


class SearchResultStub(_Output):
    id: str
    title: str
    url: str


class StandardSearchResponse(_Output):
    results: list[SearchResultStub]


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
        expected_attendance=_estimate(values.get("expected_attendance")),
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
        vibe_tags=list(values.get("vibe_tags") or []),
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
        price_min=row.get("price_min"),
        price_max=row.get("price_max"),
        booking_url=row.get("booking_url"),
        registration_required=row.get("registration_required"),
        kind=row["kind"],
        event_status=row["event_status"],
        confidence=row["confidence"],
        match_score=row["match_score"],
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
            price_min=event.get("price_min"),
            price_max=event.get("price_max"),
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


def _validated_filters(body: QueryBody) -> tuple[SearchFilters, dict[str, float]]:
    from eventindex.api.search import SOFT_ATTRIBUTES

    values = body.model_dump()
    importance = values.pop("importance")
    if not all(
        name in SOFT_ATTRIBUTES and 0 <= weight <= 1
        for name, weight in importance.items()
    ):
        raise ValueError("importance must use known attributes with weights 0..1")
    return SearchFilters(**values), importance


@mcp.tool(title="Search Linz events", annotations=_READ_ONLY)
def search_events(
    filters: QueryBody | None = None,
    limit: Annotated[int, Field(ge=1, le=100)] = 20,
    sort: Literal["relevance", "starts_at"] = "relevance",
) -> SearchEventsResponse:
    """Use this when the user wants structured discovery of public events in
    Linz or roughly 25 km around it by date, category, price, exclusions, or
    audience preferences. Do not use it for Vienna, restaurants, private
    event creation, or invitations. Omitted or false sex_service_context
    excludes known commercial sex-service contexts; only explicit true
    permits them. Unknown attributes remain visible. Ongoing events are
    labeled and placed after events that start inside the requested window."""
    from eventindex.api import app as api

    parsed, importance = _validated_filters(filters or QueryBody())
    exclude_sex = parsed.sex_service_context is not True
    payload = api._run_filters(
        parsed,
        limit=2000,
        importance=importance,
        sort=sort,
        exclude_sex_service_context=exclude_sex,
    )
    # Stable partition preserves relevance or chronology inside each group.
    # Actual starts in the window must not be buried under old ongoing spans.
    rows = [row for row in payload["occurrences"] if _credible_ongoing(row)]
    rows = sorted(rows, key=lambda row: bool(row["ongoing"]))
    return SearchEventsResponse(
        data_freshness=payload["data_freshness"],
        parsed_filters=parsed,
        importance=importance,
        pool_truncated=payload["pool_truncated"],
        occurrences=[_search_occurrence(row) for row in rows[:limit]],
    )


@mcp.tool(title="Get event details", annotations=_READ_ONLY)
def get_event(
    event_id: str,
    include_sex_service_context: bool = False,
) -> EventDetailResponse:
    """Use this when the user selected a Wasup event and wants its sanitized
    public details, future and historical occurrences, confidence estimates,
    and source provenance. Raw source claims and suppressed private-location
    evidence are never returned. A known commercial sex-service event is
    unavailable unless include_sex_service_context is explicitly true."""
    return _event_detail(event_id, include_sex=include_sex_service_context)


@mcp.tool(title="Get calendar subscription link", annotations=_READ_ONLY)
def get_calendar_link(
    category: str | None = None,
    from_dt: str | None = None,
    to_dt: str | None = None,
    min_confidence: Annotated[float | None, Field(ge=0, le=1)] = None,
    include_time_unknown: Annotated[
        bool,
        Field(
            description="Include date-only events whose actual start time is "
            "unknown. Keep false unless the user explicitly asks for them."
        ),
    ] = False,
) -> CalendarLinkResponse:
    """Use this when the user wants a read-only .ics subscription URL for
    public Linz-area events. This only builds a link; it does not subscribe,
    create calendar entries, or invite anyone. Ask for a category before
    calling rather than creating an unscoped feed. The returned feed defaults
    to timed events only and always excludes known commercial sex-service
    contexts while retaining unknown classifications."""
    if category is None:
        raise ValueError(
            "A calendar subscription requires a category so the calendar "
            "does not silently truncate a broad all-event feed. Ask the user "
            "which event category they want."
        )
    params = {
        key: value
        for key, value in [
            ("category", category),
            ("from", from_dt),
            ("to", to_dt),
            ("min_confidence", min_confidence),
        ]
        if value is not None
    }
    params["exclude_sex_service_context"] = "true"
    params["include_time_unknown"] = (
        "true" if include_time_unknown else "false"
    )
    return CalendarLinkResponse(
        ics_url=f"{BASE_URL}/v1/feed.ics?{urlencode(params)}"
    )


_STOPWORDS = {
    "a", "an", "and", "around", "at", "events", "event", "find", "for",
    "in", "index", "linz", "me", "near", "of", "please", "search", "show", "the",
    "wasup", "what", "with", "veranstaltung", "veranstaltungen", "für",
    "im", "in", "mir", "suche", "und", "zeige",
}
_SYNONYMS = {
    "running": ("running", "run", "lauf", "jogging"),
    "runs": ("run", "lauf", "running", "jogging"),
    "run": ("run", "lauf", "running", "jogging"),
    "laufen": ("lauf", "run", "running", "jogging"),
    "lauf": ("lauf", "run", "running", "jogging"),
    "concert": ("concert", "konzert"),
    "concerts": ("concert", "konzert"),
    "konzerte": ("konzert", "concert"),
    "dancing": ("dancing", "dance", "tanz"),
    "dance": ("dance", "dancing", "tanz"),
    "tanzen": ("tanz", "dance", "dancing"),
}


def _keyword_tokens(query: str) -> list[str]:
    return [
        token for token in re.findall(r"[^\W\d_]+", query.lower())
        if len(token) >= 3 and token not in _STOPWORDS
    ]


def _keyword_terms(tokens: list[str]) -> list[str]:
    terms: list[str] = []
    for token in tokens:
        for term in _SYNONYMS.get(token, (token,)):
            if term not in terms:
                terms.append(term)
    return terms


def _keyword_categories(tokens: list[str]) -> list[str] | None:
    if set(tokens) & {"running", "runs", "run", "laufen", "lauf"}:
        return ["sport"]
    if set(tokens) & {"concert", "concerts", "konzert", "konzerte"}:
        return ["music"]
    return None


def _keyword_row_matches(tokens: list[str], row: dict) -> bool:
    """Require every meaningful query token; synonyms within a token are OR.

    The SQL filter intentionally accepts any synonym so it can retrieve a
    broad candidate pool.  The standard connector contract then fails closed
    here: a query for "football lounge nights special" must not degrade into
    arbitrary events containing only "special" after the adult result is
    policy-filtered.
    """
    haystack = " ".join([
        row.get("title") or "",
        row.get("venue_name") or "",
        row.get("organizer") or "",
        " ".join(row.get("category") or []),
    ]).casefold()
    for token in tokens:
        matched = False
        for term in _SYNONYMS.get(token, (token,)):
            compound = r"[-\s]?".join(re.escape(part) for part in term.split())
            # word-prefix OR compound-suffix, matching the SQL semantics.
            if re.search(rf"(?<!\w){compound}|{compound}(?!\w)", haystack):
                matched = True
                break
        if not matched:
            return False
    return True


# Fail-closed thresholds (audit B1 successor): boolean AND guaranteed that a
# query aimed at a policy-filtered adult event could not degrade into
# arbitrary single-word filler. Under ranked OR the mean threshold does that
# job: one incidental exact hit among >=3 noise tokens stays below 0.35.
_MIN_BEST_SIM = 0.45   # at least one token must be a real lexical hit
_MIN_MEAN_SCORE = 0.35


def _haystack_words(row: dict) -> list[str]:
    text = " ".join(filter(None, [
        row.get("title"), row.get("venue_name"), row.get("venue_address"),
        row.get("organizer"), " ".join(row.get("category") or []),
    ]))
    return re.findall(r"[^\W\d_]+", text.lower())


def _token_similarity(token: str, word: str) -> float:
    if token == word:
        return 1.0
    if len(token) >= 4 and token in word:
        return 0.75  # German compounds/inflections: konzert | gartenkonzert
    ta, tb = _trigrams(token), _trigrams(word)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _rank_rows(tokens: list[str], rows: list[dict]) -> list[dict]:
    """Score = mean over query tokens of the best word similarity."""
    if not tokens:
        return []
    scored = []
    for row in rows:
        words = _haystack_words(row)
        sims = [max((_token_similarity(t, w) for w in words), default=0.0)
                for t in tokens]
        score = sum(sims) / len(sims)
        if max(sims) >= _MIN_BEST_SIM and score >= _MIN_MEAN_SCORE:
            scored.append((score, row))
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
    """Use this when the user wants keyword search over upcoming public
    Linz-area events or a client requires the standard search/fetch contract.
    Use search_events for structured dates, categories, prices, or exclusions.
    Do not use for other cities, restaurants, private events, or invitations.
    Known commercial sex-service events and past-start occurrences are always
    excluded; fewer than ten results is preferable to irrelevant filler."""
    from eventindex.api import app as api

    cutoff = datetime.now(VIENNA)
    tokens = _keyword_tokens(query)
    filters = SearchFilters(**(
        FILTER_DEFAULTS | {
            "from_dt": cutoff.isoformat(),
            "categories": _keyword_categories(tokens),
            "include_terms": _keyword_terms(tokens),
        }
    ))
    payload = api._run_filters(
        filters,
        limit=2000,
        sort="starts_at",
        distinct=True,
        exclude_sex_service_context=True,
        include_inferred_terms=False,
    )
    rows = [
        row for row in payload["occurrences"]
        if row["starts_at"] >= cutoff and _keyword_row_matches(tokens, row)
    ]
    results, seen = [], set()
    for row in rows:
        semantic_key = (
            re.sub(r"\W+", "", row["title"].casefold()),
            (row.get("venue_name") or "").casefold(),
        )
        if semantic_key in seen:
            continue
        seen.add(semantic_key)
        local = row["starts_at"].astimezone(VIENNA)
        venue = f" @ {row['venue_name']}" if row.get("venue_name") else ""
        results.append(SearchResultStub(
            id=str(row["event_id"]),
            title=f"{row['title']} ({local:%a %Y-%m-%d %H:%M}{venue})",
            url=_event_url(row["event_id"]),
        ))
        if len(results) >= 10:
            break
    return StandardSearchResponse(results=results)


def _format_estimates(estimates: EventEstimates) -> str | None:
    values = estimates.model_dump(exclude_none=True)
    values.pop("vibe_tags", None)
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
            f"Price: {event.price_min}-{event.price_max}"
            if event.price_min is not None else "Price: unknown"
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
