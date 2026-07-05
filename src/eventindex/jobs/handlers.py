"""Job handlers: pure functions (job, tx) -> [jobs to enqueue].

crawl: fetch -> extraction cascade -> event_claim rows -> enqueue resolve.
resolve: claims of one crawl -> canonical event/occurrence rows (v0).

The dummy path ({"dummy": true}) stays as the end-to-end smoke test from
phase 0.
"""

import uuid

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from eventindex import config, fetch, llm
from eventindex.budget import record_spend
from eventindex.extract import extract, parse_dt
from eventindex.resolve.fingerprint import fingerprint
from eventindex.resolve.rebuild import rebuild

YIELD_EMA_ALPHA = 0.3


class Ping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool


def _dummy_crawl(job: dict, tx) -> list[dict]:
    if config.OPENROUTER_API_KEY:
        ping = llm.complete(
            tx, 'Reply with exactly {"ok": true}.', Ping, job_id=job["id"]
        )
        detail = f"dummy crawl, llm ping ok={ping.ok}"
    else:
        record_spend(
            0.0001, "other", job_id=job["id"], detail="synthetic spend, no API key"
        )
        detail = "dummy crawl, synthetic spend (OPENROUTER_API_KEY unset)"
    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, events_found, detail) "
        "VALUES (%s, now(), 'ok', 0, %s)",
        (job["id"], detail),
    )
    return []


def _log_crawl(tx, crawl_id, job, source_id, status, events_found=0, detail=None):
    tx.execute(
        "INSERT INTO crawl_log (id, job_id, source_id, finished_at, status, "
        "events_found, detail) VALUES (%s, %s, %s, now(), %s, %s, %s)",
        (crawl_id, job["id"], source_id, status, events_found, detail),
    )


def crawl(job: dict, tx) -> list[dict]:
    if job["payload"].get("dummy"):
        return _dummy_crawl(job, tx)

    source_id = job["payload"]["source_id"]
    source = tx.execute(
        "SELECT *, ST_Y(geo) AS lat, ST_X(geo) AS lon FROM source WHERE id = %s",
        (source_id,),
    ).fetchone()
    crawl_id = uuid.uuid4()

    result = fetch.fetch_source(source)

    if result.status == fetch.BLOCKED:
        tx.execute("UPDATE source SET status = 'blocked' WHERE id = %s", (source_id,))
        _log_crawl(tx, crawl_id, job, source_id, "error", detail="robots.txt disallows")
        return []

    if result.status == fetch.UNCHANGED:
        tx.execute(
            "UPDATE source SET last_crawled = now() WHERE id = %s", (source_id,)
        )
        _log_crawl(tx, crawl_id, job, source_id, "unchanged")
        return []

    method, payloads = extract(source, result, tx, job_id=job["id"])

    for payload in payloads:
        starts = parse_dt(payload["starts_at"]["value"])
        lat = payload.get("lat", {}).get("value")
        lon = payload.get("lon", {}).get("value")
        fp = fingerprint(
            payload["title"]["value"], starts,
            lat=lat if lat is not None else source["lat"],
            lon=lon if lon is not None else source["lon"],
        )
        tx.execute(
            "INSERT INTO event_claim (source_id, crawl_id, fingerprint, payload) "
            "VALUES (%s, %s, %s, %s)",
            (source_id, crawl_id, fp, Jsonb(payload)),
        )

    tx.execute(
        """
        UPDATE source SET
            last_crawled = now(),
            last_yield = %(n)s,
            yield_ema = yield_ema * (1 - %(alpha)s) + %(n)s * %(alpha)s,
            last_content_hash = %(hash)s,
            http_etag = %(etag)s,
            http_last_modified = %(lm)s,
            extraction_hint = coalesce(extraction_hint, '{}'::jsonb)
                              || jsonb_build_object('method', %(method)s::text)
        WHERE id = %(id)s
        """,
        {
            "n": len(payloads),
            "alpha": YIELD_EMA_ALPHA,
            "hash": result.content_hash,
            "etag": result.etag,
            "lm": result.last_modified,
            "method": method,
            "id": source_id,
        },
    )
    _log_crawl(
        tx, crawl_id, job, source_id, "ok",
        events_found=len(payloads), detail=f"method={method}",
    )
    if not payloads:
        return []
    # debounce: one pending rebuild covers any number of finished crawls
    pending = tx.execute(
        "SELECT 1 FROM jobs WHERE kind = 'resolve' AND status = 'pending' LIMIT 1"
    ).fetchone()
    return [] if pending else [{"kind": "resolve", "payload": {}}]


def resolve(job: dict, tx) -> list[dict]:
    """Full canon rebuild (H0): resolve(all_claims) -> canon, atomically."""
    stats = rebuild(tx)
    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, events_found, detail) "
        "VALUES (%s, now(), 'ok', %s, %s)",
        (job["id"], stats["events"],
         f"rebuild: {stats['claims']} claims -> {stats['events']} events, "
         f"{stats['occurrences']} occurrences, {stats['venues_created']} new venues"),
    )
    return []


HANDLERS = {"crawl": crawl, "resolve": resolve}
