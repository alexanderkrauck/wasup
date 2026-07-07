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
from eventindex.fetch.recipe import Recipe, run_recipe
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

    if source["recipe"]:
        return _crawl_recipe(job, tx, source, crawl_id)

    result = fetch.fetch_source(source)

    if result.status == fetch.UNCHANGED:
        tx.execute(
            "UPDATE source SET last_crawled = now() WHERE id = %s", (source_id,)
        )
        _log_crawl(tx, crawl_id, job, source_id, "unchanged")
        return []

    method, payloads = extract(source, result, tx, job_id=job["id"])

    _insert_claims(tx, source, crawl_id, payloads)
    _update_source_stats(tx, job, source, len(payloads), method)
    tx.execute(
        "UPDATE source SET last_content_hash = %s, http_etag = %s, "
        "http_last_modified = %s WHERE id = %s",
        (result.content_hash, result.etag, result.last_modified, source_id),
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


def _insert_claims(tx, source, crawl_id, payloads) -> None:
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
            (source["id"], crawl_id, fp, Jsonb(payload)),
        )


def _update_source_stats(tx, job, source, n_payloads: int, method: str) -> None:
    tx.execute(
        """
        UPDATE source SET
            last_crawled = now(),
            last_yield = %(n)s,
            yield_ema = yield_ema * (1 - %(a)s) + %(n)s * %(a)s,
            cost_ema = cost_ema * (1 - %(a)s) + %(a)s * coalesce(
                (SELECT sum(amount_eur) FROM budget_spend WHERE job_id = %(job_id)s), 0),
            extraction_hint = coalesce(extraction_hint, '{}'::jsonb)
                              || jsonb_build_object('method', %(method)s::text)
        WHERE id = %(id)s
        """,
        {"n": n_payloads, "a": YIELD_EMA_ALPHA, "job_id": job["id"],
         "method": method, "id": source["id"]},
    )


def _crawl_recipe(job: dict, tx, source: dict, crawl_id) -> list[dict]:
    """Recipe-driven crawl (§5b) with the self-healing contract: validation
    failure OR >80% yield drop vs EMA, twice in a row -> degraded + re-onboard."""
    recipe = Recipe.model_validate(source["recipe"])
    payloads, validation = run_recipe(recipe, source, tx, job_id=job["id"])
    payloads = [p for p in payloads if "starts_at" in p and parse_dt(p["starts_at"]["value"])]

    yield_dropped = (
        source["yield_ema"] > 5 and len(payloads) < 0.2 * source["yield_ema"]
    )
    healthy = validation.ok and not yield_dropped
    hint = source["extraction_hint"] or {}
    degraded_count = 0 if healthy else hint.get("degraded_count", 0) + 1
    tx.execute(
        "UPDATE source SET extraction_hint = coalesce(extraction_hint,'{}'::jsonb) "
        "|| jsonb_build_object('degraded_count', %s::int) WHERE id = %s",
        (degraded_count, source["id"]),
    )

    _insert_claims(tx, source, crawl_id, payloads)
    _update_source_stats(tx, job, source, len(payloads), "recipe")
    detail = f"method=recipe v{source['recipe_version']}"
    if not healthy:
        detail += f" UNHEALTHY({degraded_count}): " + "; ".join(validation.reasons)[:200]
    _log_crawl(tx, crawl_id, job, source["id"], "ok" if healthy else "error",
               events_found=len(payloads), detail=detail)

    jobs = []
    if degraded_count >= 2:
        tx.execute("UPDATE source SET status = 'degraded' WHERE id = %s", (source["id"],))
        jobs.append({"kind": "onboard", "payload": {
            "source_id": str(source["id"]),
            "reason": f"self-heal: {'; '.join(validation.reasons)[:300]}",
        }})
    if payloads:
        pending = tx.execute(
            "SELECT 1 FROM jobs WHERE kind = 'resolve' AND status = 'pending' LIMIT 1"
        ).fetchone()
        if not pending:
            jobs.append({"kind": "resolve", "payload": {}})
    return jobs


ENRICH_BATCH_PER_REBUILD = 200


def resolve(job: dict, tx) -> list[dict]:
    """Full canon rebuild (H0): resolve(all_claims) -> canon, atomically."""
    stats = rebuild(tx)
    pending = stats.pop("enrich_pending", [])
    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, events_found, detail) "
        "VALUES (%s, now(), 'ok', %s, %s)",
        (job["id"], stats["events"],
         f"rebuild: {stats['claims']} claims -> {stats['events']} events, "
         f"{stats['occurrences']} occurrences, {stats['venues_created']} new venues, "
         f"{len(pending)} events awaiting enrichment"),
    )
    return [
        {"kind": "enrich", "payload": {"event_id": str(eid)}}
        for eid in pending[:ENRICH_BATCH_PER_REBUILD]
    ]


def enrich(job: dict, tx) -> list[dict]:
    """§8/H5: infer audience attributes for one event (cached by content)."""
    from eventindex.enrich import apply_to_event, enrich_event

    row = tx.execute(
        "SELECT e.id, e.title, e.description, e.category, e.price_min, "
        "e.price_max, v.name AS venue_name "
        "FROM event e LEFT JOIN venue v ON v.id = e.venue_id WHERE e.id = %s",
        (job["payload"]["event_id"],),
    ).fetchone()
    if row is None:
        return []  # event resolved away since; the next rebuild re-enqueues
    attributes = enrich_event(tx, row, job_id=job["id"])
    apply_to_event(tx, row["id"], attributes)
    return []


def onboard(job: dict, tx) -> list[dict]:
    """Recipe synthesis (§5b): one agent session; escalation ladder = worker
    retries (attempt 1 mini, later attempts mid model)."""
    from eventindex.discovery.onboard import onboard_source

    source = tx.execute(
        "SELECT *, ST_Y(geo) AS lat, ST_X(geo) AS lon FROM source WHERE id = %s",
        (job["payload"]["source_id"],),
    ).fetchone()
    model = config.MODEL_MINI if job["attempts"] <= 1 else config.MODEL_MID
    recipe = onboard_source(tx, source, job["id"], model)
    tx.execute(
        "UPDATE source SET recipe = %s, recipe_version = recipe_version + 1, "
        "status = 'active', extraction_hint = coalesce(extraction_hint,'{}'::jsonb) "
        "|| '{\"degraded_count\": 0}'::jsonb WHERE id = %s",
        (Jsonb(recipe.model_dump()), source["id"]),
    )
    return [{"kind": "crawl", "payload": {"source_id": str(source["id"])}}]


def probe(job: dict, tx) -> list[dict]:
    from eventindex.discovery.probe import probe_url

    result = probe_url(
        tx, job["payload"]["url"], job["payload"].get("discovered_via", "unknown"),
        job_id=job["id"],
    )
    if result["outcome"] == "error":
        # network/DNS/timeouts are retryable, NOT terminal - a transient
        # outage silently wiped a whole discovery batch (red-team 2,
        # 2026-07-07: 18/19 probes lost, logged as 'ok')
        raise RuntimeError(
            f"probe fetch failed for {job['payload']['url']}: {result.get('detail')}"
        )
    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, detail) "
        "VALUES (%s, now(), 'ok', %s)",
        (job["id"], f"probe {job['payload']['url'][:120]} -> {result['outcome']} "
                    f"{result.get('detail', '')}"[:400]),
    )
    if result["outcome"] == "registered":
        return [{"kind": "onboard", "payload": {"source_id": str(result["source_id"])}}]
    return []


def discover(job: dict, tx) -> list[dict]:
    from eventindex.discovery.sweep import discover as run_sweep

    channel = job["payload"]["channel"]
    seen, enqueued = run_sweep(tx, channel, job_id=job["id"])
    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, detail) "
        "VALUES (%s, now(), 'ok', %s)",
        (job["id"], f"discover[{channel}]: {seen} candidates, {enqueued} probes enqueued"),
    )
    return []


HANDLERS = {
    "crawl": crawl, "resolve": resolve, "enrich": enrich,
    "onboard": onboard, "probe": probe, "discover": discover,
}
