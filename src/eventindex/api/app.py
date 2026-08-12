"""REST API (§9): occurrences, agent search, events, .ics feed, reports,
changes. One middleware-style dependency for API keys, no auth framework.

Hard contracts in force: null means unknown (a category filter never matches
events with unknown category, by SQL semantics of && on arrays);
data_freshness in every response; projected occurrences are labeled.
Bootstrap rule: while the api_key table has no active row, the API is open.

Run: uv run uvicorn eventindex.api.app:app
"""

import base64
import json
import math
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError

from eventindex import config, db, tags as tag_store
from eventindex.budget import BudgetExceeded, DailyBudgetExceeded
from eventindex.api.confidence import (
    DEFAULT_MIN_CONFIDENCE,
    EFFECTIVE_CONFIDENCE_SQL,
)
from eventindex.api.search import QueryBody, VIENNA

MAX_LIMIT = 200

_PROVENANCE_SQL = """
    SELECT array_agg(DISTINCT s.name) FROM identity i
    JOIN event_claim c ON c.fingerprint = i.fingerprint
    JOIN source s ON s.id = c.source_id
    WHERE i.event_id = e.id AND s.kind <> 'internal'
"""

_PRICE_SOURCE_SQL = """
    SELECT coalesce(c.payload->'url'->>'value', s.url)
    FROM event_claim c JOIN source s ON s.id = c.source_id
    WHERE c.id = nullif(e.field_provenance->'price_min'->>'claim', '')::uuid
"""

# discovery surfaces stay open like /docs: they carry no data, only the
# instructions an agent needs before it has a key (the human pages fetch
# their data through the rate-limited reads)
_OPEN_PATHS = {"/", "/calendar", "/llms.txt", "/.well-known/api-catalog",
               "/privacy", "/terms", "/support", "/logo.png"}

# read-only surfaces are keyless (public data, zero LLM cost - /v1/query is
# pure Postgres by design) but rate-limited per IP. /v1/search stays keyed
# because it spends OUR llm budget per call; /v1/reports because it writes.
_PUBLIC_READS = {
    ("GET", "/v1/occurrences"), ("POST", "/v1/query"), ("GET", "/v1/query"),
    ("GET", "/v1/feed.ics"), ("GET", "/v1/changes"),
}
PUBLIC_READ_RATE_PER_MIN = 60
_rate: dict[str, list[float]] = {}  # ip -> recent request timestamps


def _client_ip(request: Request) -> str:
    # uvicorn sits behind Caddy on localhost; the real client is in XFF
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (
        request.client.host if request.client else "unknown"
    )


def _rate_limit(ip: str) -> None:
    import time as _time

    now = _time.monotonic()
    window = [t for t in _rate.get(ip, []) if now - t < 60]
    if len(window) >= PUBLIC_READ_RATE_PER_MIN:
        raise HTTPException(
            429, "rate limit: 60 requests/min without an API key",
            headers={"Retry-After": "60"},
        )
    window.append(now)
    _rate[ip] = window
    if len(_rate) > 10_000:  # bounded memory under address churn
        _rate.clear()


def _valid_key(conn, request: Request) -> bool:
    key = request.headers.get("x-api-key") or request.query_params.get("api_key")
    return bool(key) and conn.execute(
        "SELECT 1 FROM api_key WHERE key = %s AND active", (key,)
    ).fetchone() is not None


def _require_api_key(request: Request) -> None:
    if request.url.path in _OPEN_PATHS:
        return
    path = request.url.path
    is_public_read = (request.method, path) in _PUBLIC_READS or (
        request.method == "GET" and path.startswith("/v1/events/")
    )
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM api_key WHERE active LIMIT 1").fetchone() is None:
            return  # bootstrap: no keys registered yet -> open
        if _valid_key(conn, request):
            return  # keyed callers skip the anonymous rate limit
    if is_public_read:
        _rate_limit(_client_ip(request))
        return
    raise HTTPException(401, "API key required for this endpoint")


from mcp.server.fastmcp.server import StreamableHTTPASGIApp  # noqa: E402

from eventindex.api.mcp_server import mcp as _mcp  # noqa: E402

_mcp.streamable_http_app()  # initializes the session manager


@asynccontextmanager
async def _lifespan(app: FastAPI):
    # the MCP session manager needs a running task group; a plain Route's
    # lifespan is never invoked, so the parent app runs it
    async with _mcp.session_manager.run():
        yield


app = FastAPI(title="eventindex", version="v1", lifespan=_lifespan,
              dependencies=[Depends(_require_api_key)])


@app.exception_handler(BudgetExceeded)
async def _paid_budget_unavailable(_request: Request, exc: BudgetExceeded):
    """Paid convenience endpoints fail explicitly; free query stays usable."""
    detail = (
        "daily paid-provider budget exhausted; use POST /v1/query"
        if isinstance(exc, DailyBudgetExceeded)
        else "paid model provider temporarily unavailable; use POST /v1/query"
    )
    return JSONResponse(
        {"detail": detail}, status_code=503, headers={"Retry-After": "3600"}
    )
# an exact-path ASGI Route, not a Mount: mounting would 307-redirect
# POST /mcp -> /mcp/, which MCP clients do not follow
from starlette.routing import Route as _Route  # noqa: E402

app.router.routes.append(_Route(
    "/mcp", StreamableHTTPASGIApp(_mcp.session_manager),
    methods=["GET", "POST", "DELETE"],
))


@app.middleware("http")
async def _mcp_gate(request: Request, call_next):
    """Mounted apps bypass FastAPI dependencies, so /mcp gets the same
    treatment as the public reads here: keyless, rate-limited per IP,
    a valid key lifts the limit."""
    if request.url.path.startswith("/mcp"):
        with db.connect() as conn:
            keys_exist = conn.execute(
                "SELECT 1 FROM api_key WHERE active LIMIT 1"
            ).fetchone() is not None
            if keys_exist and not _valid_key(conn, request):
                try:
                    _rate_limit(_client_ip(request))
                except HTTPException as e:
                    return JSONResponse(
                        {"detail": e.detail}, status_code=e.status_code,
                        headers=e.headers,
                    )
    return await call_next(request)


@app.get("/llms.txt", include_in_schema=False)
def llms_txt():
    """llms.txt convention: the instruction document a visiting agent needs
    to use this index well (semantics, filter schema, examples)."""
    text = (Path(__file__).parent / "llms.md").read_text()
    return Response(
        text.replace("{categories}", ", ".join(config.CATEGORIES)),
        # text/plain per the llms.txt convention: some agent fetchers return
        # empty bodies for text/markdown (found by the first consumer)
        media_type="text/plain; charset=utf-8",
    )


def _page(name: str) -> Response:
    return Response((Path(__file__).parent / name).read_text(),
                    media_type="text/html; charset=utf-8")


@app.get("/", include_in_schema=False)
def landing_page():
    """Landing + install instructions (scope fence extended to landing,
    terms, privacy, support by Alexander, 2026-07-14 — plugin-directory
    submissions require them)."""
    return _page("index.html")


@app.get("/calendar", include_in_schema=False)
def calendar_page():
    """One plain HTML calendar view over the public read API (frontend scope
    fence lifted for exactly this page by Alexander, 2026-07-09)."""
    html = (Path(__file__).parent / "calendar.html").read_text()
    return Response(
        html.replace("{categories_json}", json.dumps(config.CATEGORIES)),
        media_type="text/html; charset=utf-8",
    )


@app.get("/privacy", include_in_schema=False)
def privacy():
    """GDPR-facing policy; also a plugin-directory requirement."""
    return _page("privacy.html")


@app.get("/terms", include_in_schema=False)
def terms():
    return _page("terms.html")


@app.get("/support", include_in_schema=False)
def support():
    return _page("support.html")


@app.get("/logo.png", include_in_schema=False)
def logo():
    return Response((Path(__file__).parent / "wasup-logo.png").read_bytes(),
                    media_type="image/png",
                    headers={"cache-control": "public, max-age=86400"})


@app.get("/.well-known/api-catalog", include_in_schema=False)
def api_catalog():
    """RFC 9727 API discovery: points agents at the spec and the docs."""
    return Response(
        content='{"linkset": [{"anchor": "/", '
        '"service-desc": [{"href": "/openapi.json", '
        '"type": "application/vnd.oai.openapi+json"}], '
        '"service-doc": [{"href": "/llms.txt", "type": "text/markdown"}]}]}',
        media_type="application/linkset+json",
    )


def _data_freshness(conn) -> datetime | None:
    return conn.execute(
        "SELECT max(started_at) AS ts FROM crawl_log "
        "WHERE status IN ('ok', 'unchanged')"
    ).fetchone()["ts"]


def _scale_band(participants: int) -> str:
    if participants < 30:
        return "intimate"
    if participants < 100:
        return "small"
    if participants < 300:
        return "medium"
    if participants < 1000:
        return "large"
    if participants < 5000:
        return "very_large"
    return "mass"


def _public_price(row: dict, inferred: dict | None = None) -> dict:
    inferred = inferred or row.get("inferred") or {}
    if row.get("price_min") is not None:
        confidence = (
            (row.get("field_provenance") or {})
            .get("price_min", {})
            .get("confidence", 0.8)
        )
        return {
            "min": float(row["price_min"]),
            "max": float(
                row["price_max"]
                if row.get("price_max") is not None else row["price_min"]
            ),
            "currency": "EUR",
            "confidence": float(confidence),
            "basis": "stated",
            "source_url": row.get("price_source_url") or row.get("url"),
        }
    estimate = inferred.get("price") or {}
    return {
        "min": estimate.get("min"),
        "max": estimate.get("max"),
        "currency": estimate.get("currency") or "EUR",
        "confidence": estimate.get("confidence"),
        "basis": estimate.get("basis") or "unknown",
        "source_url": None,
    }


def _public_event_scale(row: dict, inferred: dict | None = None) -> dict:
    inferred = inferred or row.get("inferred") or {}
    scale = inferred.get("event_scale") or {}
    participants = (
        row.get("expected_attendance")
        if row.get("expected_attendance") is not None
        else scale.get("estimated_participants")
    )
    if participants is None:
        return {
            "estimate_status": "unknown",
            "estimated_participants": None,
            "plausible_min": None,
            "plausible_max": None,
            "band": None,
            "confidence": None,
            "basis": [],
        }
    participants = int(participants)
    confidence = (
        row.get("expected_attendance_confidence")
        if row.get("expected_attendance_confidence") is not None
        else scale.get("confidence")
    )
    plausible_min = scale.get("plausible_min")
    plausible_max = scale.get("plausible_max")
    if (
        plausible_min is None
        or plausible_max is None
        or (
            plausible_min == plausible_max
            and (confidence is None or confidence < 0.5)
        )
    ):
        # A low-certainty point estimate is not an exact crowd count. Older
        # enrichment rows only stored the point, and models occasionally
        # repeat it as both bounds. Derive an honest confidence-scaled
        # interval for presentation; never narrow a real supplied range.
        uncertainty = max(0.15, 0.5 * (1 - float(confidence or 0)))
        plausible_min = max(1, math.floor(participants * (1 - uncertainty)))
        plausible_max = max(
            plausible_min, math.ceil(participants * (1 + uncertainty))
        )
    return {
        "estimate_status": "estimated",
        "estimated_participants": participants,
        "plausible_min": int(plausible_min),
        "plausible_max": int(plausible_max),
        "band": _scale_band(participants),
        "confidence": confidence,
        "basis": list(scale.get("basis") or ["event estimate"]),
    }


def _attach_public_price_and_scale(row: dict) -> dict:
    inferred = row.pop("inferred", None) or {}
    row["price"] = _public_price(row, inferred)
    row["event_scale"] = _public_event_scale(row, inferred)
    for key in (
        "price_min", "price_max", "price_source_url", "field_provenance",
        "expected_attendance", "expected_attendance_confidence",
    ):
        row.pop(key, None)
    return row


def _parse_radius(radius: str) -> float:
    from eventindex.api.search import _radius_m

    try:
        return _radius_m(radius)
    except ValueError as exc:
        raise HTTPException(422, str(exc))


def _parse_bbox(value: str) -> tuple[float, float, float, float]:
    try:
        pieces = value.split(",")
        if len(pieces) != 4:
            raise ValueError
        x1, y1, x2, y2 = (float(piece) for piece in pieces)
    except ValueError:
        raise HTTPException(
            422, "bbox must be 'min_lon,min_lat,max_lon,max_lat'"
        )
    if not all(math.isfinite(item) for item in (x1, y1, x2, y2)):
        raise HTTPException(422, "bbox coordinates must be finite")
    if not (-180 <= x1 <= x2 <= 180 and -90 <= y1 <= y2 <= 90):
        raise HTTPException(
            422,
            "bbox must have ordered longitude -180..180 and latitude -90..90",
        )
    return x1, y1, x2, y2


def _encode_cursor(ts: datetime, row_id) -> str:
    # URL-safe: the old raw "ts|uuid" format contained '+' and broke when
    # pasted into a query string unencoded (audit B')
    raw = f"{ts.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _parse_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
    except (ValueError, UnicodeDecodeError):
        decoded = cursor  # legacy raw cursors keep working
    try:
        ts, row_id = decoded.split("|", 1)
        return datetime.fromisoformat(ts), UUID(row_id)
    except ValueError:
        raise HTTPException(422, "invalid cursor")


def _occurrence_filter_parts(
    from_, to, near, radius, bbox, category, min_confidence,
    name=None, organizer=None, venue=None, source=None,
    exclude_sex_service_context: bool = False,
) -> tuple[list[tuple[str, str]], dict]:
    """Labelled shared filters for listings, feeds, and feed audits."""
    from eventindex.api.search import DEFAULT_RADIUS_KM, LINZ_CENTER, _lat_lon

    # overlap semantics: something still running at `from` is in the window
    # (audit A21: ongoing exhibitions were invisible from day 2)
    parts = [
        ("date_window", "coalesce(o.ends_at, o.starts_at) >= %(from)s"),
        ("not_scheduled", "o.status = 'scheduled'"),
    ]
    params: dict = {"from": from_ or datetime.now(timezone.utc)}

    if to is not None:
        parts.append(("date_window", "o.starts_at <= %(to)s"))
        params["to"] = to
    radius_norm = radius.strip().lower()
    explicit_radius = radius_norm not in ("", "any", "default")
    parsed_near = None
    if near is not None:
        try:
            parsed_near = _lat_lon(near)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
    if radius_norm != "any" and parsed_near is not None:
        lat, lon = parsed_near
        parts.append(("geography", (
            "ST_DWithin(coalesce(e.geo, v.geo)::geography, "
            "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, %(meters)s)"
        )))
        params.update(
            lat=lat, lon=lon,
            meters=_parse_radius(radius if explicit_radius else "5km"),
        )
    elif explicit_radius:  # radius without near = circle around Linz center
        parts.append(("geography", (
            "ST_DWithin(coalesce(e.geo, v.geo)::geography, "
            "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, "
            "%(meters)s)"
        )))
        params.update(lat=LINZ_CENTER[0], lon=LINZ_CENTER[1],
                      meters=_parse_radius(radius))
    elif bbox is None and radius_norm != "any":
        # default gate: the index is Linz (15km circle) - but events with
        # UNKNOWN location stay in (null = unknown, audit decision 2026-07-13)
        parts.append(("geography", (
            "(coalesce(e.geo, v.geo) IS NULL OR "
            "ST_DWithin(coalesce(e.geo, v.geo)::geography, "
            "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, "
            "%(meters)s))"
        )))
        params.update(lat=LINZ_CENTER[0], lon=LINZ_CENTER[1],
                      meters=DEFAULT_RADIUS_KM * 1000)
    if bbox is not None:
        x1, y1, x2, y2 = _parse_bbox(bbox)
        parts.append((
            "geography",
            "e.geo && ST_MakeEnvelope(%(x1)s, %(y1)s, %(x2)s, %(y2)s, 4326)",
        ))
        params.update(x1=x1, y1=y1, x2=x2, y2=y2)
    if category is not None:
        # null category = unknown: never matches a category filter (§7)
        parts.append(("category", "e.category && %(cats)s"))
        params["cats"] = [c.strip() for c in category.split(",")]
    parts.append((
        "confidence", f"({EFFECTIVE_CONFIDENCE_SQL}) >= %(min_conf)s",
    ))
    params["min_conf"] = (
        DEFAULT_MIN_CONFIDENCE
        if min_confidence is None else min_confidence
    )
    if name:
        # Same event-title scope and German compound-suffix behavior as the
        # structured query core.
        pat = r"[-\s]?".join(re.escape(tok) for tok in name.split())
        parts.append(("name", "e.title ~* %(event_name)s"))
        params["event_name"] = rf"{pat}\M"
    if organizer:
        parts.append(("organizer", "e.organizer ILIKE %(organizer_name)s"))
        params["organizer_name"] = f"%{organizer}%"
    if venue:
        parts.append(("venue", "v.name ILIKE %(venue_name)s"))
        params["venue_name"] = f"%{venue}%"
    if source:
        parts.append(("source", (
            "EXISTS (SELECT 1 FROM identity src_i "
            "JOIN event_claim src_c ON src_c.fingerprint = src_i.fingerprint "
            "JOIN source src_s ON src_s.id = src_c.source_id "
            "WHERE src_i.event_id = e.id AND src_s.kind <> 'internal' "
            "AND (src_s.name ILIKE %(source_name)s "
            "OR src_s.url ILIKE %(source_name)s))"
        )))
        params["source_name"] = f"%{source}%"
    if exclude_sex_service_context:
        # Keep unknown classifications: the MCP safety policy suppresses
        # only events positively identified as commercial sex services.
        parts.append(("sex_service_safety", (
            "coalesce(v.sex_service, false) IS DISTINCT FROM TRUE AND "
            "(e.inferred->'sex_service_context'->>'value')::bool "
            "IS DISTINCT FROM TRUE"
        )))
    return parts, params


def _occurrence_filters(
    from_, to, near, radius, bbox, category, min_confidence,
    name=None, organizer=None, venue=None, source=None,
    exclude_sex_service_context: bool = False,
) -> tuple[list[str], dict]:
    """The shared filter set of /v1/occurrences and /v1/feed.ics."""
    parts, params = _occurrence_filter_parts(
        from_, to, near, radius, bbox, category, min_confidence,
        name, organizer, venue, source, exclude_sex_service_context,
    )
    return [condition for _, condition in parts], params


@app.get("/v1/occurrences")
def occurrences(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    near: str | None = Query(None, description="lat,lon"),
    radius: str = Query("default", description="'5km'/'800m'; 'any' disables the default 15km-around-Linz gate"),
    bbox: str | None = Query(None, description="min_lon,min_lat,max_lon,max_lat"),
    category: str | None = Query(None, description="comma-separated"),
    min_confidence: float = Query(
        DEFAULT_MIN_CONFIDENCE, ge=0, le=1,
        description="effective confidence after freshness decay; set 0 to "
        "include tentative/unverified hints",
    ),
    name: str | None = Query(
        None, description="literal event-title search; German compound "
        "suffixes such as Maturaball match name=ball"),
    organizer: str | None = Query(None, description="literal organizer name"),
    venue: str | None = Query(None, description="literal venue name"),
    source: str | None = Query(
        None, description="literal reporting-source name or URL"),
    tags: str | None = Query(
        None, description="comma-separated semantic event tags; supplying "
        "tags makes this chronological listing a certainty-weighted filter"),
    min_tag_match: float = Query(0.5, ge=0, le=1),
    min_tag_concept_match: float | None = Query(None, ge=0, le=1),
    limit: int = Query(50, le=MAX_LIMIT, ge=1),
    cursor: str | None = None,
):
    """Raw chronological listing: HARD filters only (null = unknown never
    matches), keyset-paginated. For importance x certainty ranking over
    audience attributes use POST /v1/query."""
    conditions, params = _occurrence_filters(
        from_, to, near, radius, bbox, category, min_confidence,
        name, organizer, venue, source,
    )
    desired_tags = [tag.strip() for tag in (tags or "").split(",") if tag.strip()]
    if min_tag_concept_match is not None and not desired_tags:
        raise HTTPException(
            422, "min_tag_concept_match requires at least one tag"
        )
    if desired_tags:
        try:
            condition, desired_tags = tag_store.semantic_threshold_sql(
                desired_tags, min_tag_match, params, prefix="occ_tag",
                min_concept_match=min_tag_concept_match,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        conditions.append(condition)
    params["limit"] = limit
    if cursor is not None:
        after_ts, after_id = _parse_cursor(cursor)
        conditions.append("(o.starts_at, o.id) > (%(after_ts)s, %(after_id)s)")
        params.update(after_ts=after_ts, after_id=after_id)

    sql = f"""
        SELECT o.id, o.event_id, o.starts_at, o.ends_at, o.status, o.projected,
               o.availability, o.last_confirmed_at, o.time_unknown,
               (o.starts_at < %(from)s) AS ongoing,
               CASE WHEN o.time_unknown THEN e.inferred->'start_time'
                    END AS start_time_estimate,
               e.title, e.category, e.price_min, e.price_max, e.url,
               e.kind, e.organizer, e.status AS event_status,
               e.booking_url, e.registration_required,
               e.inferred, e.field_provenance,
               e.expected_attendance, e.expected_attendance_confidence,
               ({_PRICE_SOURCE_SQL}) AS price_source_url,
               v.name AS venue_name, v.address AS venue_address,
               ({EFFECTIVE_CONFIDENCE_SQL}) AS confidence,
               ST_Y(e.geo) AS lat, ST_X(e.geo) AS lon,
               ({_PROVENANCE_SQL}) AS provenance_summary
        FROM occurrence o JOIN event e ON e.id = o.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        WHERE {" AND ".join(conditions)}
        ORDER BY o.starts_at, o.id
        LIMIT %(limit)s
    """
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        if desired_tags:
            matches = tag_store.semantic_matches(
                conn, [row["event_id"] for row in rows], desired_tags
            )
            for row in rows:
                match = matches.get(row["event_id"])
                row["tag_match"] = (
                    round(match["score"], 4) if match else None
                )
                row["tag_weakest_concept_match"] = (
                    round(match["weakest_concept_score"], 4)
                    if match else None
                )
                row["tag_weakest_concept"] = (
                    match["weakest_concept_query"] if match else None
                )
                row["tag_context_match"] = (
                    round(match["combined_context_score"], 4)
                    if match and match["combined_context_score"] is not None
                    else None
                )
        rows = [_attach_public_price_and_scale(row) for row in rows]
        freshness = _data_freshness(conn)

    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = _encode_cursor(last["starts_at"], last["id"])

    return {
        "data_freshness": freshness,
        "occurrences": rows,
        "next_cursor": next_cursor,
    }


def _feed_filter_parts(
    from_, to, near, radius, bbox, category, min_confidence,
    name, organizer, venue, source, weekdays, max_price, is_free,
    participant_count_min, participant_count_max, min_scale_confidence,
    tags, min_tag_match, min_tag_concept_match,
    exclude_sex_service_context, include_time_unknown,
) -> tuple[list[tuple[str, str]], dict]:
    """One feed-membership definition shared by rendering and auditing."""
    parts, params = _occurrence_filter_parts(
        from_, to, near, radius, bbox, category, min_confidence,
        name, organizer, venue, source,
        exclude_sex_service_context,
    )
    if weekdays:
        from eventindex.api.search import WEEKDAY_NUMBERS

        desired_weekdays = [
            day.strip().lower() for day in weekdays.split(",") if day.strip()
        ]
        unknown_weekdays = set(desired_weekdays) - set(WEEKDAY_NUMBERS)
        if unknown_weekdays:
            raise HTTPException(
                422,
                f"unknown weekdays {sorted(unknown_weekdays)}; "
                f"valid: {sorted(WEEKDAY_NUMBERS)}",
            )
        parts.append(("weekday", (
            "extract(isodow from o.starts_at AT TIME ZONE 'Europe/Vienna')::int "
            "= ANY(%(feed_weekdays)s)"
        )))
        params["feed_weekdays"] = [
            WEEKDAY_NUMBERS[day] for day in desired_weekdays
        ]
    if (
        participant_count_min is not None
        and participant_count_max is not None
        and participant_count_min > participant_count_max
    ):
        raise HTTPException(
            422, "participant_count_min is greater than participant_count_max"
        )
    has_participant_bound = (
        participant_count_min is not None or participant_count_max is not None
    )
    if min_scale_confidence is not None and not has_participant_bound:
        raise HTTPException(
            422,
            "min_scale_confidence requires participant_count_min or "
            "participant_count_max",
        )
    scale_confidence = (
        0 if min_scale_confidence is None else min_scale_confidence
    )
    if not include_time_unknown:
        parts.append(("time_unknown", "NOT o.time_unknown"))
    if is_free:
        parts.append(("stated_price", "e.price_min = 0"))
    elif max_price is not None:
        parts.append(("stated_price", "e.price_min <= %(feed_max_price)s"))
        params["feed_max_price"] = max_price
    if participant_count_min is not None:
        parts.append(("event_scale", (
            "e.expected_attendance >= %(feed_scale_min)s AND "
            "e.expected_attendance_confidence >= %(feed_scale_conf)s"
        )))
        params.update(
            feed_scale_min=participant_count_min,
            feed_scale_conf=scale_confidence,
        )
    if participant_count_max is not None:
        parts.append(("event_scale", (
            "e.expected_attendance <= %(feed_scale_max)s AND "
            "e.expected_attendance_confidence >= %(feed_scale_conf)s"
        )))
        params.update(
            feed_scale_max=participant_count_max,
            feed_scale_conf=scale_confidence,
        )
    desired_tags = [tag.strip() for tag in (tags or "").split(",") if tag.strip()]
    if min_tag_concept_match is not None and not desired_tags:
        raise HTTPException(
            422, "min_tag_concept_match requires at least one tag"
        )
    if desired_tags:
        try:
            condition, desired_tags = tag_store.semantic_threshold_sql(
                desired_tags, min_tag_match, params, prefix="feed_tag",
                min_concept_match=min_tag_concept_match,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        parts.append((
            "tag_membership" if min_tag_concept_match is not None else "tag_match",
            condition,
        ))
    return parts, params


def _feed_rows(
    *, from_=None, to=None, near=None, radius="default", bbox=None,
    category=None, min_confidence=DEFAULT_MIN_CONFIDENCE, name=None,
    organizer=None, venue=None, source=None, weekdays=None, max_price=None,
    is_free=None, participant_count_min=None, participant_count_max=None,
    min_scale_confidence=None, tags=None, min_tag_match=0.5,
    min_tag_concept_match=None,
    exclude_sex_service_context=False, include_time_unknown=True, limit=500,
) -> list[dict]:
    parts, params = _feed_filter_parts(
        from_, to, near, radius, bbox, category, min_confidence,
        name, organizer, venue, source, weekdays, max_price, is_free,
        participant_count_min, participant_count_max, min_scale_confidence,
        tags, min_tag_match, min_tag_concept_match,
        exclude_sex_service_context, include_time_unknown,
    )
    params["limit"] = limit
    sql = f"""
        SELECT o.id, o.event_id, o.starts_at, o.ends_at, o.projected, o.time_unknown,
               e.title, e.url, v.name AS venue_name
        FROM occurrence o JOIN event e ON e.id = o.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        WHERE {" AND ".join(condition for _, condition in parts)}
        ORDER BY o.starts_at, o.id
        LIMIT %(limit)s
    """
    with db.connect() as conn:
        return conn.execute(sql, params).fetchall()


def _feed_coverage(occurrence_ids: list[UUID], **feed_options) -> list[dict]:
    """Explain whether highlighted occurrences survive the exact feed."""
    if not occurrence_ids:
        return []
    # One timestamp and one SQL snapshot must define both the per-filter audit
    # and limited feed membership. Recomputing a default `now()` in a second
    # query could mislabel a boundary event as truncated.
    feed_options = dict(feed_options)
    feed_options["from_"] = (
        feed_options.get("from_") or datetime.now(timezone.utc)
    )
    parts, params = _feed_filter_parts(
        feed_options.get("from_"), feed_options.get("to"),
        feed_options.get("near"), feed_options.get("radius", "default"),
        feed_options.get("bbox"), feed_options.get("category"),
        feed_options.get("min_confidence", DEFAULT_MIN_CONFIDENCE),
        feed_options.get("name"), feed_options.get("organizer"),
        feed_options.get("venue"), feed_options.get("source"),
        feed_options.get("weekdays"), feed_options.get("max_price"),
        feed_options.get("is_free"),
        feed_options.get("participant_count_min"),
        feed_options.get("participant_count_max"),
        feed_options.get("min_scale_confidence"),
        feed_options.get("tags"), feed_options.get("min_tag_match", 0.5),
        feed_options.get("min_tag_concept_match"),
        feed_options.get("exclude_sex_service_context", False),
        feed_options.get("include_time_unknown", True),
    )
    check_columns = [
        f"coalesce(({condition}), false) AS check_{index}"
        for index, (_, condition) in enumerate(parts)
    ]
    params["audit_ids"] = occurrence_ids
    params["limit"] = feed_options.get("limit", 500)
    conditions_sql = " AND ".join(condition for _, condition in parts)
    checks_sql = ", ".join(check_columns)
    audit_sql = f"""
        WITH included AS (
            SELECT o.id
            FROM occurrence o JOIN event e ON e.id = o.event_id
            LEFT JOIN venue v ON v.id = e.venue_id
            WHERE {conditions_sql}
            ORDER BY o.starts_at, o.id
            LIMIT %(limit)s
        )
        SELECT o.id, o.event_id, e.title,
               (included.id IS NOT NULL) AS included,
               {checks_sql}
        FROM occurrence o JOIN event e ON e.id = o.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        LEFT JOIN included ON included.id = o.id
        WHERE o.id = ANY(%(audit_ids)s)
    """
    with db.connect() as conn:
        audit_rows = conn.execute(audit_sql, params).fetchall()
        tag_audits = {}
        if feed_options.get("tags") \
                and feed_options.get("min_tag_concept_match") is not None:
            desired = [
                tag.strip()
                for tag in feed_options["tags"].split(",") if tag.strip()
            ]
            tag_audits = tag_store.semantic_matches(
                conn, [row["event_id"] for row in audit_rows], desired
            )
    by_id = {row["id"]: row for row in audit_rows}
    coverage = []
    for occurrence_id in occurrence_ids:
        row = by_id.get(occurrence_id)
        if row is None:
            coverage.append({
                "occurrence_id": occurrence_id, "title": None,
                "included": False, "reasons": ["not_found"],
            })
            continue
        reasons = list(dict.fromkeys(
            label
            for index, (label, _) in enumerate(parts)
            if not row[f"check_{index}"]
        ))
        if "tag_membership" in reasons:
            match = tag_audits.get(row["event_id"], {})
            final_score = round(float(match.get("score", 0)), 4)
            weakest_score = round(
                float(match.get("weakest_concept_score", 0)), 4
            )
            reasons.remove("tag_membership")
            if final_score < feed_options.get("min_tag_match", 0.5):
                reasons.append("tag_match")
            if weakest_score < feed_options["min_tag_concept_match"]:
                reasons.append("tag_concept_match")
        if not reasons and not row["included"]:
            reasons = ["feed_limit"]
        coverage.append({
            "occurrence_id": occurrence_id,
            # The audit accepts caller-supplied UUIDs. Do not turn that into
            # a title lookup for events the mandatory MCP safety gate would
            # otherwise suppress.
            "title": (
                None if "sex_service_safety" in reasons else row["title"]
            ),
            "included": bool(row["included"]),
            "reasons": reasons,
        })
    return coverage


@app.get("/v1/feed.ics")
def feed_ics(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    near: str | None = Query(None, description="lat,lon"),
    radius: str = Query("default", description="'5km'/'800m'; 'any' disables the default 15km-around-Linz gate"),
    bbox: str | None = Query(None, description="min_lon,min_lat,max_lon,max_lat"),
    category: str | None = Query(None, description="comma-separated"),
    min_confidence: float = Query(
        DEFAULT_MIN_CONFIDENCE, ge=0, le=1,
        description="effective confidence after freshness decay; set 0 to "
        "include tentative/unverified hints",
    ),
    name: str | None = Query(None, description="literal event-title search"),
    organizer: str | None = Query(None, description="literal organizer name"),
    venue: str | None = Query(None, description="literal venue name"),
    source: str | None = Query(
        None, description="literal reporting-source name or URL"),
    weekdays: str | None = Query(
        None, description="comma-separated local weekdays, e.g. thursday,friday"),
    max_price: float | None = Query(
        None, ge=0, description="hard maximum stated EUR price"),
    is_free: bool | None = Query(
        None, description="true requires an explicitly free event"),
    participant_count_min: int | None = Query(None, ge=1),
    participant_count_max: int | None = Query(None, ge=1),
    min_scale_confidence: float | None = Query(
        None, ge=0, le=1,
        description="requires participant_count_min or participant_count_max",
    ),
    tags: str | None = Query(
        None, description="comma-separated semantic event tags"),
    min_tag_match: float = Query(
        0.5, ge=0, le=1,
        description="minimum certainty-weighted semantic tag match"),
    min_tag_concept_match: float | None = Query(
        None, ge=0, le=1,
        description="optional minimum match for every requested tag concept"),
    exclude_sex_service_context: bool = Query(
        False,
        description="exclude events positively identified as taking place "
        "in a commercial sex-service context; unknown remains included",
    ),
    include_time_unknown: bool = Query(
        True,
        description="include date-only events whose start time is unknown; "
        "set false for a quieter timed-events-only calendar",
    ),
    limit: int = Query(500, le=1000, ge=1),
):
    """Any filter combo as a calendar subscription (§9)."""
    from icalendar import Calendar, Event as ICalEvent

    rows = _feed_rows(
        from_=from_, to=to, near=near, radius=radius, bbox=bbox,
        category=category, min_confidence=min_confidence, name=name,
        organizer=organizer, venue=venue, source=source, weekdays=weekdays,
        max_price=max_price, is_free=is_free,
        participant_count_min=participant_count_min,
        participant_count_max=participant_count_max,
        min_scale_confidence=min_scale_confidence, tags=tags,
        min_tag_match=min_tag_match,
        min_tag_concept_match=min_tag_concept_match,
        exclude_sex_service_context=exclude_sex_service_context,
        include_time_unknown=include_time_unknown, limit=limit,
    )

    cal = Calendar()
    cal.add("prodid", "-//eventindex//linz//")
    cal.add("version", "2.0")
    for r in rows:
        ev = ICalEvent()
        ev.add("uid", f"{r['id']}@eventindex")
        ev.add("summary", r["title"] + (" (unbestätigt)" if r["projected"] else ""))
        if r["time_unknown"]:
            # date-only sources: an all-day entry beats a fake midnight
            # (and must use the business timezone: Vienna midnight is still
            # the previous date in UTC during DST).
            local_start = r["starts_at"].astimezone(VIENNA).date()
            ev.add("dtstart", local_start)
            if r["ends_at"]:
                # RFC 5545 requires DTSTART/DTEND value types to match and an
                # all-day DTEND is exclusive. Source date ranges are inclusive.
                local_end = r["ends_at"].astimezone(VIENNA).date()
                ev.add("dtend", max(
                    local_start + timedelta(days=1),
                    local_end + timedelta(days=1),
                ))
        else:
            ev.add("dtstart", r["starts_at"])
            if r["ends_at"]:
                ev.add("dtend", r["ends_at"])
        if r["venue_name"]:
            ev.add("location", r["venue_name"])
        if r["url"]:
            ev.add("url", r["url"])
        cal.add_component(ev)
    return Response(content=cal.to_ical(), media_type="text/calendar")


def _run_filters(filters, limit: int,
                 importance: dict[str, float] | None = None,
                 sort: str = "relevance", distinct: bool = False,
                 offset: int = 0,
                 exclude_sex_service_context: bool = False,
                 ) -> dict:
    """The deterministic search core shared by /v1/search and /v1/query.

    Discovery selects one relevant occurrence per event before ranking and
    limiting, so recurring events cannot crowd later one-offs out of the
    candidate set.
    """
    from eventindex.api.search import attribute_select, build_sql, rank

    with db.connect() as conn:
        where, params = build_sql(
            filters,
            exclude_sex_service_context=exclude_sex_service_context,
        )
        select = f"""
            SELECT o.id, o.event_id, o.starts_at, o.ends_at, o.status,
                   o.projected, o.time_unknown,
                   (o.starts_at < %(from)s) AS ongoing,
                   CASE WHEN o.time_unknown THEN e.inferred->'start_time'
                        END AS start_time_estimate,
                   e.title, e.category, e.price_min, e.price_max, e.url,
                   e.kind, e.organizer, e.status AS event_status,
                   e.booking_url, e.registration_required,
                   v.name AS venue_name, v.address AS venue_address,
                   e.expected_age_range AS age_range,
                   e.inferred, e.field_provenance,
                   e.expected_attendance, e.expected_attendance_confidence,
                   ({_PRICE_SOURCE_SQL}) AS price_source_url,
                   ({EFFECTIVE_CONFIDENCE_SQL}) AS confidence,
                   ST_Y(e.geo) AS lat, ST_X(e.geo) AS lon,
                   ({_PROVENANCE_SQL}) AS provenance_summary,
                   {attribute_select()}
            FROM occurrence o JOIN event e ON e.id = o.event_id
            LEFT JOIN venue v ON v.id = e.venue_id
            WHERE {where}
        """
        if distinct:
            sql = (
                "WITH event_candidates AS ("
                "SELECT DISTINCT ON (event_id) * FROM ("
                + select
                + ") eligible "
                "ORDER BY event_id, ongoing, starts_at, id"
                ") SELECT * FROM event_candidates ORDER BY starts_at, id"
            )
        else:
            sql = select + " ORDER BY o.starts_at, o.id"
        rows = conn.execute(sql, params).fetchall()
        tag_matches = tag_store.semantic_matches(
            conn, [row["event_id"] for row in rows], filters.tags
        ) if filters.tags else {}
        tag_scores = {
            event_id: match["score"]
            for event_id, match in tag_matches.items()
        }
        freshness = _data_freshness(conn)
    for r in rows:
        r["age_range"] = str(r["age_range"]) if r["age_range"] else None
        match = tag_matches.get(r["event_id"], {}) if filters.tags else {}
        r["tag_matches"] = match.get("concepts", [])
        r["tag_weakest_concept_match"] = (
            round(match["weakest_concept_score"], 4) if match else None
        )
        r["tag_weakest_concept"] = (
            match.get("weakest_concept_query") if match else None
        )
        r["tag_context_match"] = (
            round(match["combined_context_score"], 4)
            if match and match.get("combined_context_score") is not None
            else None
        )
    # The MCP safety policy already enforces known commercial-sex exclusion
    # in SQL. Do not also reward a positive "known safe" classification over
    # unknown rows: that hidden preference can dilute the activity/topic the
    # caller actually requested.
    ranking_filters = (
        filters.model_copy(update={"sex_service_context": None})
        if exclude_sex_service_context and filters.sex_service_context is not True
        else filters
    )
    ranked = rank(rows, ranking_filters, importance, tag_scores)
    if sort == "starts_at":
        ranked = sorted(ranked, key=lambda r: r["starts_at"])
    ranked = [_attach_public_price_and_scale(row) for row in ranked]
    return {
        "data_freshness": freshness,
        "parsed_filters": filters.model_dump(),
        "importance": importance or {},
        "pool_truncated": False,
        "occurrences": ranked[offset:offset + limit],
    }


@app.get("/v1/search")
def search(q: str, limit: int = Query(20, le=100, ge=1)):
    """Natural-language search: a mini model parses the query into hard
    filters (costs the index LLM budget - agents should POST /v1/query
    with the filters instead). The parsed filters are echoed."""
    from eventindex.api.search import parse_query

    with db.connect() as conn:
        filters = parse_query(conn, q)  # spend is ledgered on its own connection
    return _run_filters(filters, limit)


@app.get("/v1/query")
def query_get(
    request: Request,
    limit: int = Query(20, le=100, ge=1),
    offset: int = Query(0, ge=0, le=2000),
    sort: Literal["relevance", "starts_at"] = "relevance",
    distinct: Literal["event", "occurrence"] = "event",
):
    """GET variant of /v1/query for browse-only agents (ChatGPT's browsing
    tool cannot POST). Same filters as query params: lists comma-separated
    (name=ball), importance as importance=attr:0.9,attr2:0.4.
    """
    from eventindex.api.search import FILTER_DEFAULTS

    body: dict = {}
    importance: dict = {}
    for name, raw in request.query_params.items():
        if name in ("limit", "api_key", "offset", "sort", "distinct"):
            continue
        if name == "importance":
            try:
                importance = {
                    k: float(v) for k, v in
                    (pair.split(":", 1) for pair in raw.split(",") if pair)
                }
            except ValueError:
                raise HTTPException(422, "importance format: attr:0.9,attr2:0.4")
        elif name not in FILTER_DEFAULTS:
            raise HTTPException(422, f"unknown filter '{name}'")
        elif isinstance(FILTER_DEFAULTS[name], list) or name == "categories":
            body[name] = [v.strip() for v in raw.split(",") if v.strip()]
        elif raw.lower() in ("true", "false"):
            body[name] = raw.lower() == "true"
        else:
            body[name] = raw
    body["importance"] = importance
    try:
        parsed = QueryBody(**body)
    except ValidationError as e:
        raise HTTPException(422, f"invalid filters: {e}")
    return query(parsed, limit, offset, sort, distinct)


@app.post("/v1/query")
def query(
    body: QueryBody,
    limit: Annotated[int, Query(le=100, ge=1)] = 20,
    offset: Annotated[int, Query(
        ge=0, le=2000, description="skip N ranked rows",
    )] = 0,
    sort: Annotated[Literal["relevance", "starts_at"], Query(
        description="relevance = certainty-aware match_score, with whole-event "
        "confidence as a tie-break (the default, NOT chronological); "
        "starts_at = chronological",
    )] = "relevance",
    distinct: Annotated[Literal["event", "occurrence"], Query(
        description="event = one row per event (its best occurrence) for "
        "discovery queries; occurrence = every date separately",
    )] = "event",
):
    """Structured search for agents: send SearchFilters fields directly
    (all optional - see /llms.txt) and NO LLM runs on the index side.

    Semantics: exclude_*/window/categories/price and required_attributes are
    HARD set logic (null = unknown never matches them). All other audience
    attributes are SOFT preferences ranked by importance x stored certainty,
    anchored at the coin flip (match 0.5+c/2, contradiction 0.5-c/2, unknown
    0.45) - nothing is silently dropped; match_score exposes the weighting.
    Occurrences with projected=true are forward-projected estimates.
    """
    from eventindex.api.search import SOFT_ATTRIBUTES, SearchFilters

    data = body.model_dump()
    importance = data.pop("importance")
    if not all(k in SOFT_ATTRIBUTES and 0 <= v <= 1 for k, v in importance.items()):
        raise HTTPException(
            422,
            f"importance must map attribute names {sorted(SOFT_ATTRIBUTES)} to 0..1",
        )
    try:
        filters = SearchFilters(**data)
    except ValidationError as e:
        raise HTTPException(422, f"invalid filters: {e}")
    return _run_filters(filters, limit, importance,
                        sort=sort, distinct=distinct == "event", offset=offset)


def _safe_estimates(inferred: dict | None) -> dict:
    """Expose estimate values and certainties without raw evidence snippets.

    Evidence originates in source text and can repeat a private address or
    personal contact that the canonical projection intentionally suppressed.
    """
    safe: dict = {}
    for name, value in (inferred or {}).items():
        if name in {"price", "event_scale", "stated_price"}:
            continue
        if isinstance(value, dict):
            safe[name] = {
                "value": value.get("value"),
                "confidence": value.get("confidence"),
            }
        else:
            safe[name] = {"value": value, "confidence": None}
    return safe


def _event_detail(event_id: UUID, *, include_policy_marker: bool = False) -> dict:
    """Sanitized public event detail; raw append-only claims never leave DB."""
    with db.connect() as conn:
        row = conn.execute(
            f"""
            SELECT e.id, e.kind, e.parent_event_id, e.title, e.description,
                   e.rights, e.category, e.is_recurring, e.rrule,
                   e.registration_required, e.registration_deadline,
                   e.booking_url, e.late_entry_ok,
                   e.price_min, e.price_max, e.url, e.image_url, e.lang,
                   e.expected_age_range, e.expected_age_range_confidence,
                   e.expected_gender_split,
                   e.expected_gender_split_confidence,
                   e.expected_attendance, e.expected_attendance_confidence,
                   e.inferred, e.field_provenance,
                   ({_PRICE_SOURCE_SQL}) AS price_source_url,
                   e.confidence, e.status,
                   e.first_seen, e.last_seen, e.updated_at, e.organizer,
                   v.name AS venue_name, v.address AS venue_address,
                   v.sex_service AS venue_sex_service,
                   ST_Y(coalesce(e.geo, v.geo)) AS lat,
                   ST_X(coalesce(e.geo, v.geo)) AS lon,
                   ({_PROVENANCE_SQL}) AS provenance_summary
            FROM event e LEFT JOIN venue v ON v.id = e.venue_id
            WHERE e.id = %s
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "event not found")
        inferred = row.pop("inferred") or {}
        sex_service_context = row.pop("venue_sex_service") is True or (
            inferred.get("sex_service_context", {}).get("value") is True
        )
        row["price"] = _public_price(row, inferred)
        row["event_scale"] = _public_event_scale(row, inferred)
        for key in (
            "price_min", "price_max", "price_source_url", "field_provenance",
            "expected_attendance", "expected_attendance_confidence",
        ):
            row.pop(key, None)
        row["estimates"] = _safe_estimates(inferred)
        row["tags"] = tag_store.public_for_event(conn, event_id)
        if row.get("expected_age_range") is not None:
            row["expected_age_range"] = str(row["expected_age_range"])
        occurrences = conn.execute(
            "SELECT id, starts_at, ends_at, status, projected, availability, "
            "waitlist_url, fullness_estimate, last_confirmed_at, time_unknown "
            "FROM occurrence WHERE event_id = %s "
            "ORDER BY starts_at",
            (event_id,),
        ).fetchall()
        sources = conn.execute(
            """
            SELECT name, url, extracted_at
            FROM (
                SELECT DISTINCT ON (s.id)
                       s.name,
                       CASE
                           WHEN c.payload->'url'->>'value' ~* '^https?://'
                           THEN c.payload->'url'->>'value'
                           ELSE s.url
                       END AS url,
                       max(c.extracted_at) OVER (PARTITION BY s.id) AS extracted_at
                FROM identity i
                JOIN event_claim c ON c.fingerprint = i.fingerprint
                JOIN source s ON s.id = c.source_id
                WHERE i.event_id = %s AND s.kind <> 'internal'
                ORDER BY s.id,
                         (c.payload->'url'->>'value' ~* '^https?://') DESC,
                         c.extracted_at DESC
            ) AS latest_per_source
            ORDER BY extracted_at DESC
            """,
            (event_id,),
        ).fetchall()
        freshness = _data_freshness(conn)

    detail = {
        "data_freshness": freshness,
        "event": row,
        "occurrences": occurrences,
        "sources": sources,
    }
    if include_policy_marker:
        detail["_sex_service_context"] = sex_service_context
    return detail


@app.get("/v1/events/{event_id}")
def event(event_id: UUID):
    return _event_detail(event_id)


class Report(BaseModel):
    occurrence_id: UUID
    reason: Literal["wrong", "cancelled", "duplicate"]
    note: str | None = None


@app.post("/v1/reports", status_code=202)
def report(body: Report):
    """User feedback -> QA queue -> source trust (§9)."""
    from eventindex.jobs.worker import enqueue

    with db.connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM occurrence WHERE id = %s", (body.occurrence_id,)
        ).fetchone()
        if exists is None:
            raise HTTPException(404, "occurrence not found")
        conn.execute(
            "INSERT INTO report (occurrence_id, reason, note) VALUES (%s, %s, %s)",
            (body.occurrence_id, body.reason, body.note),
        )
        enqueue(conn, "qa_check", {"occurrence_id": str(body.occurrence_id)})
        conn.commit()
    return {"status": "queued for verification"}


@app.get("/v1/changes")
def changes(since: str | None = None, limit: int = Query(100, le=500, ge=1)):
    """Delta stream for downstream consumers/agents (§9): keyset cursor over
    event.updated_at."""
    conditions, params = ["true"], {"limit": limit}
    if since is not None:
        after_ts, after_id = _parse_cursor(since)
        conditions = ["(e.updated_at, e.id) > (%(after_ts)s, %(after_id)s)"]
        params.update(after_ts=after_ts, after_id=after_id)
    with db.connect() as conn:
        rows = conn.execute(
            f"""
            SELECT e.id, e.title, e.status, e.category, e.url, e.confidence,
                   e.updated_at
            FROM event e WHERE {" AND ".join(conditions)}
            ORDER BY e.updated_at, e.id LIMIT %(limit)s
            """,
            params,
        ).fetchall()
        freshness = _data_freshness(conn)
    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = _encode_cursor(last["updated_at"], last["id"])
    return {"data_freshness": freshness, "events": rows, "next_cursor": next_cursor}
