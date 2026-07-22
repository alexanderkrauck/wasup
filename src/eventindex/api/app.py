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
from eventindex.api.search import QueryBody, VIENNA

MAX_LIMIT = 200

_PROVENANCE_SQL = """
    SELECT array_agg(DISTINCT s.name) FROM identity i
    JOIN event_claim c ON c.fingerprint = i.fingerprint
    JOIN source s ON s.id = c.source_id
    WHERE i.event_id = e.id AND s.kind <> 'internal'
"""

# §7 staleness decay, computed at query time: each missed re-confirmation
# cadence multiplies confidence by 0.9. A dead pipeline fades to an empty
# feed instead of serving frozen confidence.
_EFFECTIVE_CONFIDENCE_SQL = """
    e.confidence * power(0.9, least(50, greatest(0, floor(
        extract(epoch from now() - o.last_confirmed_at)
        / nullif(extract(epoch from coalesce(e.expected_cadence, interval '7 days')), 0)
    ))))
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


def _parse_radius(radius: str) -> float:
    m = re.fullmatch(r"([\d.]+)\s*(km|m)?", radius.strip())
    if not m:
        raise HTTPException(422, "radius must look like '5km' or '500m'")
    return float(m.group(1)) * (1000 if (m.group(2) or "km") == "km" else 1)


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


def _occurrence_filters(
    from_, to, near, radius, bbox, category, min_confidence,
    include_terms=None, exclude_sex_service_context: bool = False,
) -> tuple[list[str], dict]:
    """The shared filter set of /v1/occurrences and /v1/feed.ics."""
    from eventindex.api.search import DEFAULT_RADIUS_KM, LINZ_CENTER

    # overlap semantics: something still running at `from` is in the window
    # (audit A21: ongoing exhibitions were invisible from day 2)
    conditions = [
        "coalesce(o.ends_at, o.starts_at) >= %(from)s",
        "o.status != 'cancelled'",
    ]
    params: dict = {"from": from_ or datetime.now(timezone.utc)}

    if to is not None:
        conditions.append("o.starts_at <= %(to)s")
        params["to"] = to
    radius_norm = radius.strip().lower()
    explicit_radius = radius_norm not in ("", "any", "default")
    if near is not None:
        try:
            lat, lon = (float(x) for x in near.split(","))
        except ValueError:
            raise HTTPException(422, "near must be 'lat,lon'")
        conditions.append(
            "ST_DWithin(coalesce(e.geo, v.geo)::geography, "
            "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, %(meters)s)"
        )
        params.update(
            lat=lat, lon=lon,
            meters=_parse_radius(radius if explicit_radius else "5km"),
        )
    elif explicit_radius:  # radius without near = circle around Linz center
        conditions.append(
            "ST_DWithin(coalesce(e.geo, v.geo)::geography, "
            "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, "
            "%(meters)s)"
        )
        params.update(lat=LINZ_CENTER[0], lon=LINZ_CENTER[1],
                      meters=_parse_radius(radius))
    elif bbox is None and radius_norm != "any":
        # default gate: the index is Linz (15km circle) - but events with
        # UNKNOWN location stay in (null = unknown, audit decision 2026-07-13)
        conditions.append(
            "(coalesce(e.geo, v.geo) IS NULL OR "
            "ST_DWithin(coalesce(e.geo, v.geo)::geography, "
            "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, "
            "%(meters)s))"
        )
        params.update(lat=LINZ_CENTER[0], lon=LINZ_CENTER[1],
                      meters=DEFAULT_RADIUS_KM * 1000)
    if bbox is not None:
        try:
            x1, y1, x2, y2 = (float(v) for v in bbox.split(","))
        except ValueError:
            raise HTTPException(422, "bbox must be 'min_lon,min_lat,max_lon,max_lat'")
        conditions.append("e.geo && ST_MakeEnvelope(%(x1)s, %(y1)s, %(x2)s, %(y2)s, 4326)")
        params.update(x1=x1, y1=y1, x2=x2, y2=y2)
    if category is not None:
        # null category = unknown: never matches a category filter (§7)
        conditions.append("e.category && %(cats)s")
        params["cats"] = [c.strip() for c in category.split(",")]
    if min_confidence is not None:
        conditions.append(f"({_EFFECTIVE_CONFIDENCE_SQL}) >= %(min_conf)s")
        params["min_conf"] = min_confidence
    if include_terms:
        # same word-boundary + hyphen/compound semantics as /v1/query, so
        # exhaustive text listings can page with the cursor (audit B5)
        alts = []
        for i, term in enumerate(
            t.strip() for t in include_terms.split(",") if t.strip()
        ):
            key = f"inc_term_{i}"
            pat = r"[-\s]?".join(re.escape(tok) for tok in term.split())
            alts.append(
                f"e.title ~* %({key})s "
                f"OR coalesce(v.name ~* %({key})s, false) "
                f"OR coalesce(e.organizer ~* %({key})s, false)"
            )
            params[key] = rf"\m{pat}|{pat}\M"
        if alts:
            conditions.append("(" + " OR ".join(alts) + ")")
    if exclude_sex_service_context:
        # Keep unknown classifications: the MCP safety policy suppresses
        # only events positively identified as commercial sex services.
        conditions.append(
            "coalesce(v.sex_service, false) IS DISTINCT FROM TRUE AND "
            "(e.inferred->'sex_service_context'->>'value')::bool "
            "IS DISTINCT FROM TRUE"
        )
    return conditions, params


@app.get("/v1/occurrences")
def occurrences(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    near: str | None = Query(None, description="lat,lon"),
    radius: str = Query("default", description="'5km'/'800m'; 'any' disables the default 15km-around-Linz gate"),
    bbox: str | None = Query(None, description="min_lon,min_lat,max_lon,max_lat"),
    category: str | None = Query(None, description="comma-separated"),
    min_confidence: float | None = Query(None, ge=0, le=1),
    include_terms: str | None = Query(
        None, description="comma-separated synonyms; at least one must "
        "match title/venue/organizer (word-boundary aware)"),
    tags: str | None = Query(
        None, description="comma-separated semantic event tags; supplying "
        "tags makes this chronological listing a certainty-weighted filter"),
    min_tag_match: float = Query(0.5, ge=0, le=1),
    limit: int = Query(50, le=MAX_LIMIT, ge=1),
    cursor: str | None = None,
):
    """Raw chronological listing: HARD filters only (null = unknown never
    matches), keyset-paginated. For importance x certainty ranking over
    audience attributes use POST /v1/query."""
    conditions, params = _occurrence_filters(
        from_, to, near, radius, bbox, category, min_confidence, include_terms
    )
    desired_tags = [tag.strip() for tag in (tags or "").split(",") if tag.strip()]
    if desired_tags:
        try:
            condition, desired_tags = tag_store.semantic_threshold_sql(
                desired_tags, min_tag_match, params, prefix="occ_tag"
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
               v.name AS venue_name, v.address AS venue_address,
               ({_EFFECTIVE_CONFIDENCE_SQL}) AS confidence,
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
            tag_scores = tag_store.semantic_scores(
                conn, [row["event_id"] for row in rows], desired_tags
            )
            rows = [
                row for row in rows
                if tag_scores.get(row["event_id"], 0.0) >= min_tag_match
            ]
            for row in rows:
                row["tag_match"] = round(tag_scores[row["event_id"]], 4)
            rows = rows[:limit]
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


@app.get("/v1/feed.ics")
def feed_ics(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    near: str | None = Query(None, description="lat,lon"),
    radius: str = Query("default", description="'5km'/'800m'; 'any' disables the default 15km-around-Linz gate"),
    bbox: str | None = Query(None, description="min_lon,min_lat,max_lon,max_lat"),
    category: str | None = Query(None, description="comma-separated"),
    min_confidence: float | None = Query(None, ge=0, le=1),
    include_terms: str | None = Query(None, description="comma-separated"),
    tags: str | None = Query(
        None, description="comma-separated semantic event tags"),
    min_tag_match: float = Query(
        0.5, ge=0, le=1,
        description="minimum certainty-weighted semantic tag match"),
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

    conditions, params = _occurrence_filters(
        from_, to, near, radius, bbox, category, min_confidence, include_terms,
        exclude_sex_service_context,
    )
    if not include_time_unknown:
        conditions.append("NOT o.time_unknown")
    desired_tags = [tag.strip() for tag in (tags or "").split(",") if tag.strip()]
    if desired_tags:
        try:
            condition, desired_tags = tag_store.semantic_threshold_sql(
                desired_tags, min_tag_match, params, prefix="feed_tag"
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        conditions.append(condition)
    params["limit"] = limit
    sql = f"""
        SELECT o.id, o.event_id, o.starts_at, o.ends_at, o.projected, o.time_unknown,
               e.title, e.url, v.name AS venue_name
        FROM occurrence o JOIN event e ON e.id = o.event_id
        LEFT JOIN venue v ON v.id = e.venue_id
        WHERE {" AND ".join(conditions)}
        ORDER BY o.starts_at, o.id
        LIMIT %(limit)s
    """
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()

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
                 include_tag_terms: bool = True,
                 ) -> dict:
    """The deterministic search core shared by /v1/search and /v1/query:
    guarantees as set logic in SQL; soft attribute preferences scored as
    importance x certainty; unified tags rank or explicitly threshold."""
    from eventindex.api.search import attribute_select, build_sql, rank

    with db.connect() as conn:
        where, params = build_sql(
            filters,
            exclude_sex_service_context=exclude_sex_service_context,
            include_tag_terms=include_tag_terms,
        )
        # soft preferences reorder, so the pool must cover the WHOLE window -
        # a small pool ordered by starts_at would only ever score the first
        # days (bit us live: a 14-day running query missed day-3 events). At
        # Linz scale a full window fits; the flag below keeps us honest.
        pool = 2000
        params["limit"] = pool + 1
        rows = conn.execute(
            f"""
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
                   ({_EFFECTIVE_CONFIDENCE_SQL}) AS confidence,
                   ST_Y(e.geo) AS lat, ST_X(e.geo) AS lon,
                   ({_PROVENANCE_SQL}) AS provenance_summary,
                   {attribute_select()}
            FROM occurrence o JOIN event e ON e.id = o.event_id
            LEFT JOIN venue v ON v.id = e.venue_id
            WHERE {where}
            ORDER BY o.starts_at LIMIT %(limit)s
            """,
            params,
        ).fetchall()
        tag_scores = tag_store.semantic_scores(
            conn, [row["event_id"] for row in rows], filters.tags
        ) if filters.tags else {}
        freshness = _data_freshness(conn)
    truncated = len(rows) > pool
    rows = rows[:pool]
    for r in rows:
        r["age_range"] = str(r["age_range"]) if r["age_range"] else None
    ranked = rank(rows, filters, importance, tag_scores)
    if distinct:
        # one row per EVENT (its best-ranked occurrence): a recurring event
        # filled 6 of 10 slots of a discovery query (audit B1)
        seen_events: set = set()
        ranked = [r for r in ranked
                  if not (r["event_id"] in seen_events
                          or seen_events.add(r["event_id"]))]
    if sort == "starts_at":
        ranked = sorted(ranked, key=lambda r: r["starts_at"])
    return {
        "data_freshness": freshness,
        "parsed_filters": filters.model_dump(),
        "importance": importance or {},
        # true = the window holds more rows than the ranking pool; results
        # beyond the first `pool` by start time were not scored - narrow the
        # window or add hard filters
        "pool_truncated": truncated,
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
    distinct: Literal["event", "occurrence"] = "occurrence",
):
    """GET variant of /v1/query for browse-only agents (ChatGPT's browsing
    tool cannot POST). Same filters as query params: lists comma-separated
    (include_terms=lauf,run), importance as importance=attr:0.9,attr2:0.4.
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
        ge=0, le=2000, description="skip N ranked rows (pool is 2000)",
    )] = 0,
    sort: Annotated[Literal["relevance", "starts_at"], Query(
        description="relevance = match_score x confidence (the default, NOT "
        "chronological); starts_at = chronological",
    )] = "relevance",
    distinct: Annotated[Literal["event", "occurrence"], Query(
        description="event = one row per event (its best occurrence) for "
        "discovery queries; occurrence = every date separately",
    )] = "occurrence",
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
        if name == "stated_price" and isinstance(value, dict):
            safe[name] = {
                "min": value.get("min"),
                "max": value.get("max"),
                "currency": value.get("currency"),
                "confidence": value.get("confidence"),
            }
        elif isinstance(value, dict):
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
                   e.inferred, e.confidence, e.status,
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
