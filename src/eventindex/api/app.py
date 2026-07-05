"""REST API (§9), phase 1 slice: /v1/occurrences + /v1/events/{id}.

Hard contracts already in force: null means unknown (a category filter never
matches events with unknown category, by SQL semantics of && on arrays);
data_freshness in every response. Staleness decay, auth, and semantic search
come in later phases.

Run: uv run uvicorn eventindex.api.app:app
"""

import re
from datetime import datetime, timezone
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query

from eventindex import db

app = FastAPI(title="eventindex", version="v1")

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
        ts, occ_id = cursor.split("|", 1)
        return datetime.fromisoformat(ts), UUID(occ_id)
    except ValueError:
        raise HTTPException(422, "invalid cursor")


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
    conditions = ["o.starts_at >= %(from)s", "o.status != 'cancelled'"]
    params: dict = {"from": from_ or datetime.now(timezone.utc), "limit": limit}

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
    if cursor is not None:
        after_ts, after_id = _parse_cursor(cursor)
        conditions.append("(o.starts_at, o.id) > (%(after_ts)s, %(after_id)s)")
        params.update(after_ts=after_ts, after_id=after_id)

    sql = f"""
        SELECT o.id, o.event_id, o.starts_at, o.ends_at, o.status,
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
            "SELECT id, starts_at, ends_at, status, availability, "
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
