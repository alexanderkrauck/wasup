"""Job handlers: pure functions (job, tx) -> [jobs to enqueue].

crawl: fetch -> extraction cascade -> event_claim rows -> enqueue resolve.
resolve: full canon rebuild, resolve(all_claims) -> event/occurrence (H0).
enrich/onboard/probe/discover/qa_check: see each handler.

The dummy path ({"dummy": true}) stays as the end-to-end smoke test from
phase 0.
"""

import uuid
from typing import Literal

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
    _update_source_stats(tx, job, source, payloads, method)
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


def _claim_horizon_days(payloads: list[dict]) -> float | None:
    """How far into the future this crawl's yield reaches (completeness
    contract: a productive source stuck at a short horizon = capped feed)."""
    from datetime import datetime, timezone

    dates = [parse_dt(p["starts_at"]["value"]) for p in payloads if "starts_at" in p]
    dates = [d for d in dates if d is not None]
    if not dates:
        return None
    return round((max(dates) - datetime.now(timezone.utc)).total_seconds() / 86400, 1)


def _update_source_stats(tx, job, source, payloads: list, method: str) -> None:
    horizon = _claim_horizon_days(payloads)
    tx.execute(
        """
        UPDATE source SET
            last_crawled = now(),
            -- a dormant source that yields again has earned its way back
            -- (dormancy must not be a one-way door; the monthly pulse crawl
            -- is pointless if its findings can't reactivate)
            status = CASE WHEN status = 'dormant' AND %(n)s > 0
                          THEN 'active' ELSE status END,
            last_yield = %(n)s,
            yield_ema = yield_ema * (1 - %(a)s) + %(n)s * %(a)s,
            cost_ema = cost_ema * (1 - %(a)s) + %(a)s * coalesce(
                (SELECT sum(amount_eur) FROM budget_spend WHERE job_id = %(job_id)s), 0),
            extraction_hint = coalesce(extraction_hint, '{}'::jsonb)
                              || jsonb_build_object('method', %(method)s::text)
                              || CASE WHEN %(horizon)s::float IS NULL THEN '{}'::jsonb
                                 ELSE jsonb_build_object('horizon_days', %(horizon)s::float) END
        WHERE id = %(id)s
        """,
        {"n": len(payloads), "a": YIELD_EMA_ALPHA, "job_id": job["id"],
         "method": method, "id": source["id"], "horizon": horizon},
    )


def _crawl_recipe(job: dict, tx, source: dict, crawl_id) -> list[dict]:
    """Recipe-driven crawl (§5b) with the self-healing contract: validation
    failure OR >80% yield drop vs EMA, twice in a row -> degraded + re-onboard."""
    from eventindex.extract import sanity_filter

    recipe = Recipe.model_validate(source["recipe"])
    payloads, validation = run_recipe(recipe, source, tx, job_id=job["id"])
    # selector-extracted payloads skip the cascade, so the deterministic
    # gates (future date, no placeholder title) must run here - otherwise
    # recipes insert past events and inflate yield/horizon stats
    payloads = sanity_filter(payloads, source)

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
    _update_source_stats(tx, job, source, payloads, "recipe")
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
    reason = job["payload"].get("reason")
    min_horizon = (config.RECIPE_MIN_HORIZON_DAYS
                   if reason and "completeness" in reason else None)
    recipe = onboard_source(tx, source, job["id"], model,
                            task_reason=reason, min_horizon_days=min_horizon)
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


QA_SOURCE_URL = "internal://qa-verifier"


class QAVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    outcome: Literal["confirmed", "cancelled", "not_found"]


def _qa_source_id(tx):
    """The QA verifier is a claim author like any source (H0: canon writes go
    through claims), but never scheduled (status 'internal')."""
    row = tx.execute("SELECT id FROM source WHERE url = %s", (QA_SOURCE_URL,)).fetchone()
    if row:
        return row["id"]
    return tx.execute(
        "INSERT INTO source (name, url, kind, tier, trust, status) "
        "VALUES ('QA verifier', %s, 'internal', 1, 0.9, 'internal') RETURNING id",
        (QA_SOURCE_URL,),
    ).fetchone()["id"]


def _qa_verify(tx, occ: dict, job_id) -> str:
    """Re-fetch the event's URL and ask a mini model whether the occurrence
    still stands. Checking is easier than extracting (H1.1 logic)."""
    import time

    import httpx

    from eventindex.extract.llm_text import html_to_text
    from eventindex.resolve.fingerprint import VIENNA

    try:
        with httpx.Client(
            timeout=30, follow_redirects=True,
            headers={"User-Agent": config.USER_AGENT},
        ) as client:
            time.sleep(config.CRAWL_DELAY_S)
            resp = client.get(occ["url"])
            resp.raise_for_status()
    except httpx.HTTPError:
        return "not_found"

    local = occ["starts_at"].astimezone(VIENNA)
    return llm.complete(
        tx,
        f"Below is the text of {occ['url']}.\n"
        f"Does this page still show the event '{occ['title']}' taking place "
        f"on {local:%Y-%m-%d} (around {local:%H:%M})?\n"
        "Answer 'confirmed' if the event on that date is still listed as "
        "happening; 'cancelled' if it is explicitly cancelled or moved "
        "(abgesagt, verschoben, entfällt, ausverkauft is NOT cancelled); "
        "'not_found' if the event or that date no longer appears.\n\n"
        f"PAGE TEXT:\n{html_to_text(resp.content)[:8000]}",
        QAVerdict,
        job_id=job_id,
    ).outcome


def _qa_claim(tx, qa_sid, occ: dict, status: str | None) -> None:
    """QA writes claims, never canon (H0): a positive claim re-anchors
    last_seen/last_confirmed_at at the next rebuild, a negative one flips
    the occurrence through the ordinary asymmetric status merge."""
    fp = tx.execute(
        "SELECT fingerprint FROM identity WHERE event_id = %s "
        "ORDER BY fingerprint LIMIT 1", (occ["event_id"],),
    ).fetchone()
    if fp is None:
        return
    payload = {
        "title": {"value": occ["title"], "confidence": 0.9},
        "starts_at": {"value": occ["starts_at"].isoformat(), "confidence": 0.9},
    }
    if status:
        payload["status"] = {"value": status, "confidence": 0.9}
    tx.execute(
        "INSERT INTO event_claim (source_id, fingerprint, payload) "
        "VALUES (%s, %s, %s)",
        (qa_sid, fp["fingerprint"], Jsonb(payload)),
    )


def qa_check(job: dict, tx) -> list[dict]:
    """QA loop (§12): re-verify occurrences against their event URL, feed
    source trust, and flip cancellations - via a claim, never by editing
    canon (H0)."""
    payload = job["payload"]
    base_sql = (
        "SELECT o.id, o.event_id, o.starts_at, e.title, e.url "
        "FROM occurrence o JOIN event e ON e.id = o.event_id "
    )
    if payload.get("occurrence_id"):
        occs = tx.execute(
            base_sql + "WHERE o.id = %s AND e.url IS NOT NULL",
            (payload["occurrence_id"],),
        ).fetchall()
    else:
        occs = tx.execute(
            base_sql + "WHERE o.starts_at BETWEEN now() AND now() + interval "
            "'14 days' AND o.status = 'scheduled' AND NOT o.projected "
            "AND e.url IS NOT NULL ORDER BY random() LIMIT %s",
            (payload.get("sample", config.QA_NIGHTLY_SAMPLE),),
        ).fetchall()

    qa_sid = _qa_source_id(tx)
    counts = {"confirmed": 0, "cancelled": 0, "not_found": 0}
    for occ in occs:
        outcome = _qa_verify(tx, occ, job["id"])
        counts[outcome] += 1
        accuracy = 1.0 if outcome == "confirmed" else 0.0
        tx.execute(
            "UPDATE source SET trust = trust * (1 - %(a)s) + %(a)s * %(acc)s "
            "WHERE id != %(qa)s AND id IN ("
            "  SELECT DISTINCT c.source_id FROM identity i "
            "  JOIN event_claim c ON c.fingerprint = i.fingerprint "
            "  WHERE i.event_id = %(eid)s)",
            {"a": config.QA_TRUST_ALPHA, "acc": accuracy, "qa": qa_sid,
             "eid": occ["event_id"]},
        )
        if outcome == "confirmed":
            # instant freshness for the API; the claim below is what makes
            # the confirmation survive rebuilds (canon is rebuilt from claims)
            tx.execute(
                "UPDATE occurrence SET last_confirmed_at = now() WHERE id = %s",
                (occ["id"],),
            )
            _qa_claim(tx, qa_sid, occ, status=None)
        elif outcome == "cancelled":
            _qa_claim(tx, qa_sid, occ, status="cancelled")

    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, events_found, detail) "
        "VALUES (%s, now(), 'ok', %s, %s)",
        (job["id"], len(occs),
         f"qa: checked={len(occs)} confirmed={counts['confirmed']} "
         f"cancelled={counts['cancelled']} not_found={counts['not_found']}"),
    )
    return [{"kind": "resolve", "payload": {}}] if counts["cancelled"] else []


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
    "qa_check": qa_check,
}
