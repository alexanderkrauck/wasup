"""REST API (§9): occurrences, agent search, events, .ics feed, reports,
changes. One middleware-style dependency for API keys, no auth framework.

Hard contracts in force: null means unknown (a category filter never matches
events with unknown category, by SQL semantics of && on arrays);
data_freshness in every response; projected occurrences are labeled.
Bootstrap rule: while the api_key table has no active row, the API is open.

Run: uv run uvicorn eventindex.api.app:app
"""

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ValidationError

from eventindex import config, db

MAX_LIMIT = 200

_PROVENANCE_SQL = """
    SELECT array_agg(DISTINCT s.name) FROM identity i
    JOIN event_claim c ON c.fingerprint = i.fingerprint
    JOIN source s ON s.id = c.source_id
    WHERE i.event_id = e.id
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
# instructions an agent needs before it has a key
_OPEN_PATHS = {"/llms.txt", "/.well-known/api-catalog"}


def _require_api_key(request: Request) -> None:
    if request.url.path in _OPEN_PATHS:
        return
    with db.connect() as conn:
        if conn.execute("SELECT 1 FROM api_key WHERE active LIMIT 1").fetchone() is None:
            return  # bootstrap: no keys registered yet -> open
        key = request.headers.get("x-api-key") or request.query_params.get("api_key")
        if not key or conn.execute(
            "SELECT 1 FROM api_key WHERE key = %s AND active", (key,)
        ).fetchone() is None:
            raise HTTPException(401, "missing or invalid API key")


app = FastAPI(title="eventindex", version="v1",
              dependencies=[Depends(_require_api_key)])


@app.get("/llms.txt", include_in_schema=False)
def llms_txt():
    """llms.txt convention: the instruction document a visiting agent needs
    to use this index well (semantics, filter schema, examples)."""
    text = (Path(__file__).parent / "llms.md").read_text()
    return Response(
        text.replace("{categories}", ", ".join(config.CATEGORIES)),
        media_type="text/markdown",
    )


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


def _parse_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        ts, row_id = cursor.split("|", 1)
        return datetime.fromisoformat(ts), UUID(row_id)
    except ValueError:
        raise HTTPException(422, "invalid cursor")


def _occurrence_filters(
    from_, to, near, radius, bbox, category, min_confidence
) -> tuple[list[str], dict]:
    """The shared filter set of /v1/occurrences and /v1/feed.ics."""
    conditions = ["o.starts_at >= %(from)s", "o.status != 'cancelled'"]
    params: dict = {"from": from_ or datetime.now(timezone.utc)}

    if to is not None:
        conditions.append("o.starts_at <= %(to)s")
        params["to"] = to
    if near is not None:
        try:
            lat, lon = (float(x) for x in near.split(","))
        except ValueError:
            raise HTTPException(422, "near must be 'lat,lon'")
        conditions.append(
            "ST_DWithin(e.geo::geography, "
            "ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography, %(meters)s)"
        )
        params.update(lat=lat, lon=lon, meters=_parse_radius(radius))
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
    return conditions, params


@app.get("/v1/occurrences")
def occurrences(
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = None,
    near: str | None = Query(None, description="lat,lon"),
    radius: str = "5km",
    bbox: str | None = Query(None, description="min_lon,min_lat,max_lon,max_lat"),
    category: str | None = Query(None, description="comma-separated"),
    min_confidence: float | None = None,
    limit: int = Query(50, le=MAX_LIMIT, ge=1),
    cursor: str | None = None,
):
    conditions, params = _occurrence_filters(
        from_, to, near, radius, bbox, category, min_confidence
    )
    params["limit"] = limit
    if cursor is not None:
        after_ts, after_id = _parse_cursor(cursor)
        conditions.append("(o.starts_at, o.id) > (%(after_ts)s, %(after_id)s)")
        params.update(after_ts=after_ts, after_id=after_id)

    sql = f"""
        SELECT o.id, o.event_id, o.starts_at, o.ends_at, o.status, o.projected,
               o.availability, o.last_confirmed_at,
               e.title, e.category, e.price_min, e.price_max, e.url,
               ({_EFFECTIVE_CONFIDENCE_SQL}) AS confidence,
               ST_Y(e.geo) AS lat, ST_X(e.geo) AS lon,
               ({_PROVENANCE_SQL}) AS provenance_summary
        FROM occurrence o JOIN event e ON e.id = o.event_id
        WHERE {" AND ".join(conditions)}
        ORDER BY o.starts_at, o.id
        LIMIT %(limit)s
    """
    with db.connect() as conn:
        rows = conn.execute(sql, params).fetchall()
        freshness = _data_freshness(conn)

    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = f"{last['starts_at'].isoformat()}|{last['id']}"

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
    radius: str = "5km",
    bbox: str | None = Query(None, description="min_lon,min_lat,max_lon,max_lat"),
    category: str | None = Query(None, description="comma-separated"),
    min_confidence: float | None = None,
    limit: int = Query(500, le=1000, ge=1),
):
    """Any filter combo as a calendar subscription (§9)."""
    from icalendar import Calendar, Event as ICalEvent

    conditions, params = _occurrence_filters(
        from_, to, near, radius, bbox, category, min_confidence
    )
    params["limit"] = limit
    sql = f"""
        SELECT o.id, o.starts_at, o.ends_at, o.projected,
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
                 importance: dict[str, float] | None = None) -> dict:
    """The deterministic search core shared by /v1/search and /v1/query:
    guarantees as set logic in SQL; soft attribute preferences scored as
    importance x certainty; vibe terms only rank."""
    from eventindex.api.search import attribute_select, build_sql, rank

    with db.connect() as conn:
        where, params = build_sql(filters)
        # soft preferences reorder, so the candidate pool must be wider than
        # the page - at Linz scale a whole window fits comfortably
        params["limit"] = max(limit * 3, 300)
        rows = conn.execute(
            f"""
            SELECT o.id, o.event_id, o.starts_at, o.ends_at, o.status,
                   o.projected,
                   e.title, e.category, e.price_min, e.price_max, e.url,
                   e.inferred->'vibe_tags' AS vibe_tags,
                   e.expected_age_range AS age_range,
                   ({_EFFECTIVE_CONFIDENCE_SQL}) AS confidence,
                   ST_Y(e.geo) AS lat, ST_X(e.geo) AS lon,
                   ({_PROVENANCE_SQL}) AS provenance_summary,
                   {attribute_select()}
            FROM occurrence o JOIN event e ON e.id = o.event_id
            WHERE {where}
            ORDER BY o.starts_at LIMIT %(limit)s
            """,
            params,
        ).fetchall()
        freshness = _data_freshness(conn)
    for r in rows:
        r["age_range"] = str(r["age_range"]) if r["age_range"] else None
    return {
        "data_freshness": freshness,
        "parsed_filters": filters.model_dump(),
        "importance": importance or {},
        "occurrences": rank(rows, filters, importance)[:limit],
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


@app.post("/v1/query")
def query(body: dict, limit: int = Query(20, le=100, ge=1)):
    """Structured search for agents: send SearchFilters fields directly
    (all optional - see /llms.txt) and NO LLM runs on the index side.

    Semantics: exclude_*/window/categories/price and required_attributes are
    HARD set logic (null = unknown never matches them). All other audience
    attributes are SOFT preferences ranked by importance x stored certainty
    (unknown scores a fixed prior instead of being dropped). Optional body
    key `importance`: {attribute: 0..1} weights, default 1.0 each.
    Occurrences with projected=true are forward-projected estimates.
    """
    from eventindex.api.search import ATTRIBUTES, FILTER_DEFAULTS, SearchFilters

    importance = body.pop("importance", None) or {}
    if not isinstance(importance, dict) or not all(
        k in ATTRIBUTES and isinstance(v, (int, float)) and 0 <= v <= 1
        for k, v in importance.items()
    ):
        raise HTTPException(
            422, f"importance must map attribute names {sorted(ATTRIBUTES)} to 0..1"
        )
    try:
        filters = SearchFilters(**(FILTER_DEFAULTS | body))
    except (ValidationError, TypeError) as e:
        raise HTTPException(422, f"invalid filters: {e}")
    return _run_filters(filters, limit, importance)


@app.get("/v1/events/{event_id}")
def event(event_id: UUID):
    with db.connect() as conn:
        row = conn.execute(
            f"""
            SELECT e.*, ST_Y(e.geo) AS lat, ST_X(e.geo) AS lon,
                   ({_PROVENANCE_SQL}) AS provenance_summary
            FROM event e WHERE e.id = %s
            """,
            (event_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "event not found")
        del row["geo"], row["vibe_embedding"]
        occurrences = conn.execute(
            "SELECT id, starts_at, ends_at, status, projected, availability, "
            "last_confirmed_at FROM occurrence WHERE event_id = %s "
            "ORDER BY starts_at",
            (event_id,),
        ).fetchall()
        claims = conn.execute(
            """
            SELECT c.id, s.name AS source, c.extracted_at, c.payload
            FROM identity i
            JOIN event_claim c ON c.fingerprint = i.fingerprint
            JOIN source s ON s.id = c.source_id
            WHERE i.event_id = %s ORDER BY c.extracted_at DESC
            """,
            (event_id,),
        ).fetchall()
        freshness = _data_freshness(conn)

    return {
        "data_freshness": freshness,
        "event": row,
        "occurrences": occurrences,
        "claims": claims,
    }


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
        next_cursor = f"{last['updated_at'].isoformat()}|{last['id']}"
    return {"data_freshness": freshness, "events": rows, "next_cursor": next_cursor}
