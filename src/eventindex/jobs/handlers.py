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
        "SELECT 1 FROM jobs WHERE kind = 'resolve' AND status = 'pending' "
        "AND run_after <= now() + interval '10 minutes' LIMIT 1"
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
        raw_excerpt = (
            (payload.get("title") or {}).get("event_excerpt")
            or " | ".join(filter(None, [
                (payload.get("title") or {}).get("evidence"),
                (payload.get("starts_at") or {}).get("evidence"),
            ]))
            or None
        )
        tx.execute(
            "INSERT INTO event_claim (source_id, crawl_id, fingerprint, "
            "raw_excerpt, payload) VALUES (%s, %s, %s, %s, %s)",
            (
                source["id"], crawl_id, fp,
                raw_excerpt[:2000] if raw_excerpt else None,
                Jsonb(payload),
            ),
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


AGENT_COOLDOWN_DAYS = 7  # escalation heuristics never storm the agent


def _agent_cooldown_over(hint: dict) -> bool:
    from datetime import datetime, timedelta, timezone

    last = hint.get("last_agent_extract")
    if not last:
        return True
    try:
        ts = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(timezone.utc) - ts > timedelta(days=AGENT_COOLDOWN_DAYS)


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
    # completeness heuristic: a recipe can be formally healthy yet reach a
    # fraction of what the agent SAW on the site (poster/PDF halves, capped
    # windows). Two consecutive shortfalls send the agent, cooldown-limited.
    expected = hint.get("expected_events") or 0
    low_yield = (healthy and expected >= 8
                 and len(payloads) < 0.25 * expected)
    low_yield_count = hint.get("low_yield_count", 0) + 1 if low_yield else 0
    tx.execute(
        "UPDATE source SET extraction_hint = coalesce(extraction_hint,'{}'::jsonb) "
        "|| jsonb_build_object('degraded_count', %s::int, "
        "'low_yield_count', %s::int) WHERE id = %s",
        (degraded_count, low_yield_count, source["id"]),
    )

    _insert_claims(tx, source, crawl_id, payloads)
    _update_source_stats(tx, job, source, payloads, "recipe")
    detail = f"method=recipe v{source['recipe_version']}"
    if validation.truncated and payloads:
        # a productive source hit a hard limit: events exist beyond the cap
        # and were NOT indexed - the digest turns this into a loud warning
        detail += f" LIMIT-TRUNCATED: {validation.truncated[:150]}"
    if not healthy:
        detail += f" UNHEALTHY({degraded_count}): " + "; ".join(validation.reasons)[:200]
    _log_crawl(tx, crawl_id, job, source["id"], "ok" if healthy else "error",
               events_found=len(payloads), detail=detail)

    jobs = []
    if degraded_count >= 2:
        # the agent both extracts (index never dark) and repairs the recipe
        tx.execute("UPDATE source SET status = 'degraded' WHERE id = %s", (source["id"],))
        jobs.append({"kind": "agent_extract", "payload": {
            "source_id": str(source["id"]),
            "reason": f"self-heal: {'; '.join(validation.reasons)[:300]}",
        }})
    elif low_yield_count >= 2 and _agent_cooldown_over(hint):
        jobs.append({"kind": "agent_extract", "payload": {
            "source_id": str(source["id"]),
            "reason": (f"completeness: recipe extracts {len(payloads)} events "
                       f"but you estimated ~{expected} on this site - find "
                       "and extract what the recipe misses (posters? PDFs? "
                       "deeper pages?)"),
        }})
    if payloads:
        pending = tx.execute(
            "SELECT 1 FROM jobs WHERE kind = 'resolve' AND status = 'pending' "
            "AND run_after <= now() + interval '10 minutes' LIMIT 1"
        ).fetchone()
        if not pending:
            jobs.append({"kind": "resolve", "payload": {}})
    return jobs


def resolve(job: dict, tx) -> list[dict]:
    """Full canon rebuild (H0): resolve(all_claims) -> canon, atomically."""
    stats = rebuild(tx)
    if stats.get("skipped"):
        # the concurrent rebuild covers older claims but not ours: retry
        # once it released the lock. One pending resolve is sufficient for
        # every claim committed while that rebuild runs; lock losers must not
        # create an unbounded retry fan-out.
        from datetime import datetime, timedelta, timezone

        pending = tx.execute(
            "SELECT 1 FROM jobs WHERE kind = 'resolve' AND status = 'pending' "
            "LIMIT 1"
        ).fetchone()
        if pending:
            return []
        return [{"kind": "resolve", "payload": {},
                 "run_after": datetime.now(timezone.utc) + timedelta(minutes=15)}]
    pending = stats.pop("enrich_pending", [])
    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, events_found, detail) "
        "VALUES (%s, now(), 'ok', %s, %s)",
        (job["id"], stats["events"],
         f"rebuild: {stats['claims']} claims -> {stats['events']} events, "
         f"{stats['occurrences']} occurrences, {stats['venues_created']} new venues, "
         f"{len(pending)} events awaiting enrichment"),
    )
    # Collapse all pre-existing requests to one newest follow-up. That one is
    # deliberately retained: claims may have committed after _load_claims()
    # while this long rebuild was running, and one later rebuild publishes
    # all of them. Without coalescing, every lock loser becomes a 50-minute
    # full rebuild once the advisory lock is free.
    pending_resolves = tx.execute(
        "SELECT id FROM jobs WHERE kind = 'resolve' AND status = 'pending' "
        "ORDER BY created_at DESC, id DESC"
    ).fetchall()
    if len(pending_resolves) > 1:
        keep = pending_resolves[0]["id"]
        tx.execute(
            "UPDATE jobs SET status = 'done', finished_at = now(), "
            "last_error = %s WHERE kind = 'resolve' AND status = 'pending' "
            "AND id <> %s",
            (f"superseded by atomic rebuild {job['id']}", keep),
        )
    already_queued = {
        row["event_id"]
        for row in tx.execute(
            "SELECT payload->>'event_id' AS event_id FROM jobs "
            "WHERE kind = 'enrich' AND status IN ('pending', 'running') "
            "AND payload ? 'event_id'"
        )
    }
    next_starts = {
        str(row["event_id"]): row["next_start"].isoformat()
        for row in tx.execute(
            "SELECT event_id, min(starts_at) AS next_start FROM occurrence "
            "WHERE event_id = ANY(%s) AND status = 'scheduled' "
            "AND coalesce(ends_at, starts_at) >= now() GROUP BY event_id",
            (pending,),
        )
    } if pending else {}
    jobs = [
        {
            "kind": "enrich",
            "payload": {
                "event_id": str(eid),
                **(
                    {"next_start": next_starts[str(eid)]}
                    if str(eid) in next_starts else {}
                ),
            },
        }
        for eid in pending if str(eid) not in already_queued
    ]
    jobs.append({"kind": "embed_tags", "payload": {}})
    return jobs


def enrich(job: dict, tx) -> list[dict]:
    """§8/H5: infer audience attributes for one event (cached by content)."""
    from eventindex.enrich import apply_to_event, content_key, enrich_event

    row = tx.execute(
        "SELECT e.id, e.title, e.description, e.category, e.price_min, "
        "e.price_max, v.name AS venue_name, v.sex_service AS venue_sex_service "
        "FROM event e LEFT JOIN venue v ON v.id = e.venue_id WHERE e.id = %s",
        (job["payload"]["event_id"],),
    ).fetchone()
    if row is None:
        return []  # event resolved away since; the next rebuild re-enqueues
    attributes = enrich_event(tx, row, job_id=job["id"])
    apply_to_event(tx, row["id"], attributes, enrichment_key=content_key(row))
    # Debounced derived-cache convergence: the current batch may finish while
    # more enrichment jobs create new tags, so a later enrichment re-arms it.
    pending = tx.execute(
        "SELECT 1 FROM jobs WHERE kind = 'embed_tags' "
        "AND status IN ('pending', 'running') LIMIT 1"
    ).fetchone()
    return [] if pending else [{"kind": "embed_tags", "payload": {}}]


TAG_EMBED_BATCH = 256


def embed_tags(job: dict, tx) -> list[dict]:
    """Fill the derived local vector cache in bounded idempotent batches."""
    from eventindex import embeddings

    rows = tx.execute(
        """
        SELECT DISTINCT et.name
        FROM event_tag et
        LEFT JOIN tag_embedding te
          ON te.name = et.name AND te.model = %s
        WHERE te.name IS NULL
        ORDER BY et.name
        LIMIT %s
        """,
        (embeddings.MODEL_VERSION, TAG_EMBED_BATCH),
    ).fetchall()
    if not rows:
        return []
    embeddings.store_missing(tx, [row["name"] for row in rows])
    more = len(rows) == TAG_EMBED_BATCH
    return [{"kind": "embed_tags", "payload": {}}] if more else []


def onboard(job: dict, tx) -> list[dict]:
    """Recipe synthesis (§5b): one agent session; escalation ladder = worker
    retries (attempt 1 mini, later attempts mid model)."""
    from eventindex.discovery.onboard import onboard_source

    source = tx.execute(
        "SELECT *, ST_Y(geo) AS lat, ST_X(geo) AS lon FROM source WHERE id = %s",
        (job["payload"]["source_id"],),
    ).fetchone()
    model = (config.MODEL_MINI if job["attempts"] <= 1
             else config.MODEL_MID if job["attempts"] == 2
             else config.MODEL_FRONTIER)
    reason = job["payload"].get("reason")
    min_horizon = (config.RECIPE_MIN_HORIZON_DAYS
                   if reason and "completeness" in reason else None)
    result = onboard_source(tx, source, job["id"], model,
                            task_reason=reason, min_horizon_days=min_horizon)
    _store_recipe(tx, source, result)
    return [{"kind": "crawl", "payload": {"source_id": str(source["id"])}}]


def _store_recipe(tx, source, result) -> None:
    """A validated recipe returns the source to rung 1: active, health reset,
    agentic mode cleared, the agent's own yield estimate kept as the
    low-yield yardstick."""
    hint = {"degraded_count": 0, "selfheal_attempts": 0}
    if result.expected_events:
        hint["expected_events"] = result.expected_events
    tx.execute(
        "UPDATE source SET recipe = %s, recipe_version = recipe_version + 1, "
        "status = 'active', extraction_hint = "
        "(coalesce(extraction_hint,'{}'::jsonb) - 'mode') || %s::jsonb "
        "WHERE id = %s",
        (Jsonb(result.recipe.model_dump()), Jsonb(hint), source["id"]),
    )


def agent_extract(job: dict, tx) -> list[dict]:
    """Tier-D extraction rung (fence fired 2026-07-20, Alexander: human-parity
    is the requirement): the agent extracts claims directly, so the index
    never goes dark while a recipe is broken - and repairs the recipe when a
    stable one exists. Sources no recipe can express stay in agentic mode,
    governed by their monthly budget."""
    from datetime import datetime, timezone

    from eventindex.discovery.onboard import onboard_source

    source = tx.execute(
        "SELECT *, ST_Y(geo) AS lat, ST_X(geo) AS lon FROM source WHERE id = %s",
        (job["payload"]["source_id"],),
    ).fetchone()
    # vision-capable from the first attempt; frontier on the last
    model = (config.MODEL_VISION if job["attempts"] <= 2
             else config.MODEL_FRONTIER)
    result = onboard_source(tx, source, job["id"], model,
                            task_reason=job["payload"].get("reason"),
                            mode="extract")
    crawl_id = uuid.uuid4()
    if result.payloads:
        _insert_claims(tx, source, crawl_id, result.payloads)
        _update_source_stats(tx, job, source, result.payloads, "agent")
    else:
        # the scheduler keys on last_crawled; an empty agent run must not
        # look like "never tried" and re-enqueue every tick
        tx.execute("UPDATE source SET last_crawled = now() WHERE id = %s",
                   (source["id"],))

    hint = {"last_agent_extract":
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "agent_yield": len(result.payloads),
            "selfheal_attempts": 0}
    if result.expected_events:
        hint["expected_events"] = result.expected_events
    tx.execute(
        "UPDATE source SET extraction_hint = coalesce(extraction_hint, "
        "'{}'::jsonb) || %s::jsonb WHERE id = %s",
        (Jsonb(hint), source["id"]),
    )
    if result.summary:
        prior = (source["extraction_hint"] or {}).get("onboard_notes") or []
        tx.execute(
            "UPDATE source SET extraction_hint = extraction_hint || "
            "jsonb_build_object('onboard_notes', %s::jsonb) WHERE id = %s",
            (Jsonb(([result.summary] + prior)[:3]), source["id"]),
        )

    if result.recipe is not None:
        _store_recipe(tx, source, result)
    elif result.payloads:
        # no expressible recipe: the agent IS this source's extractor now
        tx.execute(
            "UPDATE source SET status = 'active', extraction_hint = "
            "extraction_hint || '{\"mode\": \"agentic\"}'::jsonb WHERE id = %s",
            (source["id"],),
        )
    _log_crawl(
        tx, crawl_id, job, source["id"], "ok",
        events_found=len(result.payloads),
        detail="method=agent" + (" +recipe" if result.recipe else "")
               + (f" | {result.summary[:150]}" if result.summary else ""),
    )
    jobs = []
    if result.recipe is not None:
        jobs.append({"kind": "crawl", "payload": {"source_id": str(source["id"])}})
    if result.payloads:
        pending = tx.execute(
            "SELECT 1 FROM jobs WHERE kind = 'resolve' AND status = 'pending' "
            "AND run_after <= now() + interval '10 minutes' LIMIT 1"
        ).fetchone()
        if not pending:
            jobs.append({"kind": "resolve", "payload": {}})
    return jobs


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
    still stands. Checking is easier than extracting (H1.1 logic).

    QA v2 (red team 2026-07-20): the verdict must also hold venue/time when
    the page shows them (a dense list page must not 'confirm' a market at
    the wrong venue with the wrong hours), and a JS page whose static text
    lacks the title gets one headless render before ruling not_found."""
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

    text = html_to_text(resp.content)
    title_head = (occ["title"] or "")[:25].lower()
    if title_head and title_head not in text.lower():
        from eventindex.fetch.headless import render_page

        rendered = render_page(occ["url"])
        if rendered:
            text = html_to_text(rendered)

    local = occ["starts_at"].astimezone(VIENNA)
    venue_line = (f" at '{occ['venue_name']}'" if occ.get("venue_name") else "")
    return llm.complete(
        tx,
        f"Below is the text of {occ['url']}.\n"
        f"Does this page still show the event '{occ['title']}' taking place "
        f"on {local:%Y-%m-%d} (around {local:%H:%M}){venue_line}?\n"
        "Answer 'confirmed' only if the event on that date is still listed "
        "as happening AND, where the page states them, its venue and time "
        "are consistent with the ones above; 'cancelled' if it is "
        "explicitly cancelled or moved (abgesagt, verschoben, entfällt - "
        "ausverkauft is NOT cancelled); 'not_found' if the event, that "
        "date, or that venue no longer appears as described.\n\n"
        f"PAGE TEXT:\n{text[:8000]}",
        QAVerdict,
        job_id=job_id,
    ).outcome


def _qa_claim(
    tx, qa_sid, occ: dict, status: str | None,
    raw_excerpt: str | None = None,
) -> None:
    """QA writes claims, never canon (H0): a positive claim re-anchors
    last_seen/last_confirmed_at at the next rebuild, a negative one flips
    the occurrence through the ordinary asymmetric status merge."""
    candidates = tx.execute(
        "SELECT i.fingerprint, c.payload FROM identity i "
        "LEFT JOIN LATERAL ("
        "  SELECT payload FROM event_claim "
        "  WHERE fingerprint = i.fingerprint ORDER BY extracted_at DESC LIMIT 1"
        ") c ON true WHERE i.event_id = %s ORDER BY i.fingerprint",
        (occ["event_id"],),
    ).fetchall()
    if not candidates:
        return
    from zoneinfo import ZoneInfo

    local_zone = ZoneInfo(config.TIMEZONE)
    target_day = occ["starts_at"].astimezone(local_zone).date()
    fp = candidates[0]
    for candidate in candidates:
        candidate_start = parse_dt(
            (candidate["payload"].get("starts_at") or {}).get("value")
        )
        if (
            candidate_start is not None
            and candidate_start.astimezone(local_zone).date() == target_day
        ):
            fp = candidate
            break
    payload = {
        "title": {"value": occ["title"], "confidence": 0.9},
        "starts_at": {"value": occ["starts_at"].isoformat(), "confidence": 0.9},
    }
    if status:
        payload["status"] = {"value": status, "confidence": 0.9}
    tx.execute(
        "INSERT INTO event_claim (source_id, fingerprint, raw_excerpt, payload) "
        "VALUES (%s, %s, %s, %s)",
        (qa_sid, fp["fingerprint"], raw_excerpt, Jsonb(payload)),
    )


def qa_check(job: dict, tx) -> list[dict]:
    """QA loop (§12): re-verify occurrences against their event URL, feed
    source trust, and flip cancellations - via a claim, never by editing
    canon (H0)."""
    payload = job["payload"]
    base_sql = (
        "SELECT o.id, o.event_id, o.starts_at, e.title, e.url, "
        "v.name AS venue_name "
        "FROM occurrence o JOIN event e ON e.id = o.event_id "
        "LEFT JOIN venue v ON v.id = e.venue_id "
    )
    if payload.get("occurrence_id"):
        occs = tx.execute(
            base_sql + "WHERE o.id = %s AND e.url IS NOT NULL",
            (payload["occurrence_id"],),
        ).fetchall()
    else:
        sample = payload.get("sample", config.QA_NIGHTLY_SAMPLE)
        projected_share = max(1, sample // 5)
        occs = tx.execute(
            base_sql + "WHERE o.starts_at BETWEEN now() AND now() + interval "
            "'14 days' AND o.status = 'scheduled' AND NOT o.projected "
            "AND e.url IS NOT NULL ORDER BY random() LIMIT %s",
            (sample - projected_share,),
        ).fetchall()
        # QA v2: projections were never verified - which is how a source's
        # announced schedule change or a summer-paused course drifts
        # silently (red team 2026-07-20). A fifth of the sample looks 2-8
        # weeks ahead at projected occurrences.
        occs += tx.execute(
            base_sql + "WHERE o.starts_at BETWEEN now() + interval '14 days' "
            "AND now() + interval '56 days' AND o.status = 'scheduled' "
            "AND o.projected AND e.url IS NOT NULL ORDER BY random() LIMIT %s",
            (projected_share,),
        ).fetchall()

    qa_sid = _qa_source_id(tx)
    counts = {"confirmed": 0, "cancelled": 0, "not_found": 0}
    not_found_sources: dict = {}
    for occ in occs:
        outcome = _qa_verify(tx, occ, job["id"])
        counts[outcome] += 1
        if outcome == "not_found":
            src = tx.execute(
                "SELECT c.source_id FROM identity i "
                "JOIN event_claim c ON c.fingerprint = i.fingerprint "
                "JOIN source s ON s.id = c.source_id "
                "WHERE i.event_id = %s AND s.kind <> 'internal' "
                "ORDER BY s.trust DESC LIMIT 1", (occ["event_id"],),
            ).fetchone()
            if src:
                key = src["source_id"]
                not_found_sources[key] = not_found_sources.get(key, 0) + 1
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
    jobs = [{"kind": "resolve", "payload": {}}] if counts["cancelled"] else []
    # QA v2: a source whose events keep vanishing from their pages has
    # moved or restructured them - the agent finds where they went
    # (red team: Nierenstammtisch relocated from /linz to /wels)
    for source_id, n in not_found_sources.items():
        if n < 3:
            continue
        src = tx.execute("SELECT extraction_hint FROM source WHERE id = %s",
                         (source_id,)).fetchone()
        if src and _agent_cooldown_over(src["extraction_hint"] or {}):
            jobs.append({"kind": "agent_extract", "payload": {
                "source_id": str(source_id),
                "reason": (f"qa drift: {n} of this source's occurrences no "
                           "longer appear on their recorded pages - find "
                           "where the events moved and re-extract"),
            }})
    return jobs


class RiskVerdict(BaseModel):
    """Evidence-bearing verdict for one risk-prioritized occurrence."""

    model_config = ConfigDict(extra="forbid")
    outcome: Literal["supported", "contradicted", "unverified"]
    actual_starts_at: str | None
    evidence: str | None


def _risk_verify(tx, occ: dict, job_id) -> RiskVerdict:
    """Verify identity/date against the canonical public page.

    Absence is never contradiction. Only a literal page quote can support or
    negate a date, keeping temporary fetch/render failures fail-open.
    """
    import time

    import httpx

    from eventindex.extract.llm_text import (
        _date_evidence_matches,
        _is_literal_quote,
        html_to_text,
    )
    from eventindex.fetch.headless import render_page
    from eventindex.resolve.fingerprint import VIENNA

    try:
        with httpx.Client(
            timeout=30, follow_redirects=True,
            headers={"User-Agent": config.USER_AGENT},
        ) as client:
            time.sleep(config.CRAWL_DELAY_S)
            resp = client.get(occ["url"])
            resp.raise_for_status()
            final_url = str(resp.url)
    except httpx.HTTPError:
        return RiskVerdict(
            outcome="unverified", actual_starts_at=None, evidence=None,
        )

    text = html_to_text(resp.content, final_url)
    title_head = (occ["title"] or "")[:25].casefold()
    if title_head and title_head not in text.casefold():
        rendered = render_page(final_url)
        if rendered:
            text = html_to_text(rendered, final_url)

    local = occ["starts_at"].astimezone(VIENNA)
    verdict = llm.complete(
        tx,
        f"Verify one public event occurrence against the page text below.\n"
        f"Claimed event: {occ['title']!r}\n"
        f"Claimed start: {local.isoformat()}\n"
        f"Page: {final_url}\n\n"
        "Return outcome='supported' only when the page explicitly supports "
        "this same event on the claimed date (and time when stated). Return "
        "outcome='contradicted' only when the page explicitly says this event "
        "is cancelled/moved OR explicitly gives it a different date/time. "
        "For a different date, actual_starts_at is the ISO value actually "
        "shown. Return outcome='unverified' for absence, ambiguity, stale "
        "yearless pages, fetch-like error text, or insufficient evidence. "
        "evidence must be the smallest verbatim contiguous quote that proves "
        "the verdict; never paraphrase. A supported or contradicted verdict "
        "without such a quote is invalid.\n\n"
        f"PAGE TEXT:\n{text[:12000]}",
        RiskVerdict,
        job_id=job_id,
    )
    if verdict.outcome == "unverified":
        return verdict
    if not _is_literal_quote(verdict.evidence, text):
        return RiskVerdict(
            outcome="unverified", actual_starts_at=None, evidence=None,
        )
    if verdict.outcome == "supported":
        ok, normalized = _date_evidence_matches(
            occ["starts_at"].isoformat(), verdict.evidence or "",
        )
        if not ok or (not occ.get("time_unknown") and "T" not in normalized):
            return RiskVerdict(
                outcome="unverified", actual_starts_at=None, evidence=None,
            )
    elif verdict.actual_starts_at is not None:
        ok, actual = _date_evidence_matches(
            verdict.actual_starts_at, verdict.evidence or "",
        )
        parsed_actual = parse_dt(actual) if ok else None
        if (
            parsed_actual is None
            or parsed_actual == occ["starts_at"]
            or (
                parsed_actual.astimezone(VIENNA).date()
                == occ["starts_at"].astimezone(VIENNA).date()
                and "T" not in actual
            )
        ):
            return RiskVerdict(
                outcome="unverified", actual_starts_at=None, evidence=None,
            )
    return verdict


def verify_event(job: dict, tx) -> list[dict]:
    """Risk-prioritized contradiction loop for legacy singleton events."""
    occurrence_id = job["payload"]["occurrence_id"]
    occ = tx.execute(
        """
        SELECT o.id, o.event_id, o.starts_at, o.time_unknown, e.title, e.url
        FROM occurrence o JOIN event e ON e.id = o.event_id
        WHERE o.id = %s AND o.status = 'scheduled'
          AND e.url ~ '^https?://'
        """,
        (occurrence_id,),
    ).fetchone()
    if occ is None:
        return []

    verdict = _risk_verify(tx, occ, job["id"])
    if verdict.outcome == "supported":
        qa_sid = _qa_source_id(tx)
        tx.execute(
            "UPDATE occurrence SET last_confirmed_at = now() WHERE id = %s",
            (occ["id"],),
        )
        _qa_claim(
            tx, qa_sid, occ, status=None,
            raw_excerpt=(verdict.evidence or "")[:2000] or None,
        )
    elif verdict.outcome == "contradicted":
        qa_sid = _qa_source_id(tx)
        _qa_claim(
            tx, qa_sid, occ, status="cancelled",
            raw_excerpt=(verdict.evidence or "")[:2000] or None,
        )
        # Explicit contradiction is calibration evidence for every external
        # source that supported this canonical event. Unverified absence is
        # deliberately not a trust penalty.
        tx.execute(
            "UPDATE source SET trust = trust * (1 - %(a)s) "
            "WHERE kind <> 'internal' AND id IN ("
            "  SELECT DISTINCT c.source_id FROM identity i "
            "  JOIN event_claim c ON c.fingerprint = i.fingerprint "
            "  WHERE i.event_id = %(eid)s)",
            {"a": config.QA_TRUST_ALPHA, "eid": occ["event_id"]},
        )

    detail = (
        f"verify_event: risk={job['payload'].get('reason', 'unspecified')} "
        f"outcome={verdict.outcome}"
        + (
            f" actual={verdict.actual_starts_at}"
            if verdict.actual_starts_at else ""
        )
        + (
            f" evidence={verdict.evidence[:240]!r}"
            if verdict.evidence else ""
        )
    )
    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, events_found, "
        "detail) VALUES (%s, now(), 'ok', 1, %s)",
        (job["id"], detail[:2000]),
    )
    return (
        [{"kind": "resolve", "payload": {}}]
        if verdict.outcome == "contradicted" else []
    )


def parity_audit(job: dict, tx) -> list[dict]:
    """Weekly human-parity check (Alexander 2026-07-20: the requirement is a
    number that is watched, not a claim made once): the agent independently
    extracts a sample of recipe-crawled sources; its finds become claims (the
    index converges immediately), the coverage ratio lands in crawl_log for
    the digest, and misses feed the source's notes."""
    from eventindex.discovery.onboard import OnboardFailed, onboard_source

    sample = job["payload"].get("sample", config.PARITY_SAMPLE)
    sources = tx.execute(
        "SELECT *, ST_Y(geo) AS lat, ST_X(geo) AS lon FROM source "
        "WHERE status = 'active' AND recipe IS NOT NULL AND yield_ema >= 3 "
        "ORDER BY random() LIMIT %s",
        (sample,),
    ).fetchall()
    audited = 0
    for source in sources:
        try:
            result = onboard_source(
                tx, source, job["id"], config.MODEL_VISION,
                task_reason=("parity audit: extract every upcoming event this "
                             "site publishes so it can be compared against "
                             "what the cheap crawler already found"),
                mode="extract",
            )
        except OnboardFailed as e:
            _log_crawl(tx, uuid.uuid4(), job, source["id"], "error",
                       detail=f"parity: session failed: {str(e)[:200]}")
            continue
        audited += 1
        known = {
            r["fingerprint"] for r in tx.execute(
                "SELECT DISTINCT fingerprint FROM event_claim "
                "WHERE source_id = %s", (source["id"],),
            )
        }
        missing_titles, hits = [], 0
        for p in result.payloads:
            starts = parse_dt(p["starts_at"]["value"])
            fp = fingerprint(p["title"]["value"], starts,
                             lat=source["lat"], lon=source["lon"])
            if fp in known:
                hits += 1
            else:
                missing_titles.append(p["title"]["value"])
        coverage = hits / len(result.payloads) if result.payloads else 1.0
        crawl_id = uuid.uuid4()
        if result.payloads:
            _insert_claims(tx, source, crawl_id, result.payloads)
        _log_crawl(
            tx, crawl_id, job, source["id"], "ok",
            events_found=len(result.payloads),
            detail=(f"parity: agent={len(result.payloads)} known={hits} "
                    f"coverage={coverage:.2f}"
                    + (f" missing={missing_titles[:3]}" if missing_titles else "")),
        )
        if coverage < config.PARITY_MIN_COVERAGE and missing_titles:
            prior = (source["extraction_hint"] or {}).get("onboard_notes") or []
            note = ("parity audit found events the recipe misses: "
                    + ", ".join(missing_titles[:5]))[:500]
            # a proven coverage miss re-arms the one-shot escalation fuses:
            # the completeness/venue contracts may fire again on evidence
            tx.execute(
                "UPDATE source SET extraction_hint = "
                "((coalesce(extraction_hint,'{}'::jsonb) "
                "- 'completeness_escalated') - 'venue_escalated') "
                "|| jsonb_build_object('onboard_notes', %s::jsonb) "
                "WHERE id = %s",
                (Jsonb(([note] + prior)[:3]), source["id"]),
            )
    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, detail) "
        "VALUES (%s, now(), 'ok', %s)",
        (job["id"], f"parity_audit: {audited}/{len(sources)} sources audited"),
    )
    pending = tx.execute(
        "SELECT 1 FROM jobs WHERE kind = 'resolve' AND status = 'pending' "
        "AND run_after <= now() + interval '10 minutes' LIMIT 1"
    ).fetchone()
    return [] if pending or not audited else [{"kind": "resolve", "payload": {}}]


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


def hydrate_event(job: dict, tx) -> list[dict]:
    """Recover missing exact facts from public detail, booking, and web pages.

    The result is an append-only claim on the event's existing fingerprint.
    Canon remains a pure resolver projection.
    """
    from eventindex.discovery.sweep import search_web
    from eventindex.enrich.facts import PublicPage, extract_facts, fetch_pages

    event_id = job["payload"]["event_id"]
    row = tx.execute(
        """
        SELECT e.id AS event_id, e.title, e.description, e.url, e.booking_url,
               e.organizer, e.price_min, e.venue_id, e.geo,
               v.name AS venue_name,
               o.starts_at, o.time_unknown,
               i.fingerprint, c.source_id, c.payload AS source_payload,
               s.url AS source_url
        FROM event e
        JOIN occurrence o ON o.event_id = e.id
        JOIN identity i ON i.event_id = e.id
        JOIN event_claim c ON c.fingerprint = i.fingerprint
        JOIN source s ON s.id = c.source_id
        LEFT JOIN venue v ON v.id = e.venue_id
        WHERE e.id = %s AND o.status = 'scheduled'
          AND coalesce(o.ends_at, o.starts_at) >= now()
          AND s.kind <> 'internal'
        ORDER BY o.starts_at, s.trust DESC, c.extracted_at DESC
        LIMIT 1
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        return []

    urls = [
        url for url in (row.get("url"), row.get("booking_url"))
        if url and url.startswith(("http://", "https://"))
    ]
    evidence_url = row.get("url") or row.get("source_url")
    stored_pages = (
        [PublicPage(evidence_url, row["description"])]
        if row.get("description")
        and evidence_url
        and evidence_url.startswith(("http://", "https://"))
        else []
    )
    # Canonical descriptions are already public source text. Inspect them
    # first: this recovers a stated price in one bounded LLM call without
    # waiting for fetch/render/search, and preserves its real source URL.
    payload, raw_excerpt = extract_facts(
        tx, row, stored_pages, job_id=job["id"]
    )
    pages = list(stored_pages)
    searched = False

    def missing() -> tuple[bool, bool, bool, bool]:
        return (
            row["price_min"] is None and "price_min" not in payload,
            row["venue_id"] is None and row["geo"] is None
            and "venue_name" not in payload,
            row["time_unknown"] and "starts_at" not in payload,
            row["booking_url"] is None and "booking_url" not in payload,
        )

    def merge_recovery(extra_payload: dict, extra_raw: str | None) -> None:
        nonlocal raw_excerpt
        recovered_price = (
            "price_min" not in payload and "price_min" in extra_payload
        )
        for key, value in extra_payload.items():
            payload.setdefault(key, value)
        if recovered_price and "url" in extra_payload:
            payload["url"] = extra_payload["url"]
        raw_excerpt = " | ".join(filter(None, [
            raw_excerpt, extra_raw
        ]))[:2000] or None

    if any(missing()):
        fetched_pages = fetch_pages(urls)
        if fetched_pages:
            pages.extend(
                page for page in fetched_pages
                if page.url not in {known.url for known in pages}
            )
            extra_payload, extra_raw = extract_facts(
                tx, row, pages, job_id=job["id"]
            )
            merge_recovery(extra_payload, extra_raw)

    needs_price, needs_venue, needs_time, needs_booking = missing()
    if needs_price or needs_venue or needs_time or needs_booking:
        local_date = row["starts_at"].date().isoformat()
        query = " ".join(filter(None, [
            f'"{row["title"]}"',
            local_date,
            row.get("venue_name"),
            row.get("organizer"),
            "Tickets Preis Eintritt Anmeldung",
        ]))
        hits = search_web(tx, query, job_id=job["id"])
        known = {page.url for page in pages} | set(urls)
        extra_pages = fetch_pages([url for url in hits if url not in known])
        if extra_pages:
            searched = True
            extra_payload, extra_raw = extract_facts(
                tx, row, pages + extra_pages, job_id=job["id"]
            )
            merge_recovery(extra_payload, extra_raw)

    if payload:
        source_payload = row.get("source_payload") or {}
        payload.setdefault(
            "title", {
                "value": row["title"],
                "confidence": (
                    (source_payload.get("title") or {}).get("confidence")
                    or 0.5
                ),
            },
        )
        payload.setdefault(
            "starts_at",
            {
                "value": row["starts_at"].isoformat(),
                "confidence": (
                    (source_payload.get("starts_at") or {}).get("confidence")
                    or 0.5
                ),
            },
        )
        tx.execute(
            "INSERT INTO event_claim (source_id, fingerprint, raw_excerpt, payload) "
            "VALUES (%s, %s, %s, %s)",
            (
                row["source_id"], row["fingerprint"], raw_excerpt,
                Jsonb(payload),
            ),
        )
    tx.execute(
        "INSERT INTO crawl_log (job_id, source_id, finished_at, status, "
        "events_found, detail) VALUES (%s, %s, now(), 'ok', %s, %s)",
        (
            job["id"], row["source_id"], 1 if payload else 0,
            f"hydrate_event: pages={len(pages)} web_search={searched} "
            f"fields={sorted(payload)}",
        ),
    )
    if not payload:
        return []
    pending = tx.execute(
        "SELECT 1 FROM jobs WHERE kind = 'resolve' AND status = 'pending' "
        "AND run_after <= now() + interval '10 minutes' LIMIT 1"
    ).fetchone()
    if pending:
        return []
    run_after = tx.execute(
        "SELECT now() + interval '10 minutes' AS ts"
    ).fetchone()["ts"]
    return [{"kind": "resolve", "payload": {}, "run_after": run_after}]


def ground_venue(job: dict, tx) -> list[dict]:
    """Ground one named venue and recover an explicitly stated capacity."""
    from eventindex.discovery.sweep import search_web
    from eventindex.enrich.facts import fetch_pages
    from eventindex.enrich.venue_facts import (
        extract_capacity,
        find_place,
        is_location_only,
    )

    venue = tx.execute(
        "SELECT id, name, address, capacity, gmaps_place_id, "
        "ST_Y(geo) AS lat, ST_X(geo) AS lon FROM venue WHERE id = %s",
        (job["payload"]["venue_id"],),
    ).fetchone()
    if venue is None:
        return []

    # Capacity recovery uses OpenRouter after the Place match.  Refuse before
    # buying Google data when the durable provider breaker is open, otherwise
    # the later 402 rolls this transaction back and the same Place is repaid.
    if venue["capacity"] is None:
        llm.ensure_openrouter_available(probe_now=True)

    place = (
        None
        if is_location_only(venue["name"]) or venue["gmaps_place_id"] is not None
        else find_place(venue, job_id=job["id"])
    )
    matched = place is not None
    if place:
        location = place["location"]
        tx.execute(
            """
            UPDATE venue SET
                address = coalesce(address, %(address)s),
                geo = coalesce(
                    geo,
                    ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)
                ),
                gmaps_place_id = coalesce(gmaps_place_id, %(place_id)s)
            WHERE id = %(id)s
            """,
            {
                "id": venue["id"],
                "address": place.get("formattedAddress"),
                "lat": location["latitude"],
                "lon": location["longitude"],
                "place_id": place["id"],
            },
        )
        venue = tx.execute(
            "SELECT id, name, address, capacity, gmaps_place_id "
            "FROM venue WHERE id = %s",
            (venue["id"],),
        ).fetchone()

    capacity = evidence = source_url = None
    # Name-only public search is not identity evidence: generic venue names
    # such as "Backstube" occur in many cities. Capacity extraction starts
    # only after a Place match (now or in an earlier grounding run).
    place_corroborated = (
        not is_location_only(venue["name"])
        and (place is not None or venue["gmaps_place_id"] is not None)
    )
    if venue["capacity"] is None and place_corroborated:
        urls = []
        if place and place.get("websiteUri"):
            urls.append(place["websiteUri"])
        query = " ".join(filter(None, [
            f'"{venue["name"]}"', venue.get("address"),
            "Kapazität Plätze Personen Fassungsvermögen",
        ]))
        urls.extend(search_web(tx, query, job_id=job["id"]))
        pages = fetch_pages(list(dict.fromkeys(urls)))
        capacity, evidence, source_url = extract_capacity(
            tx, venue, pages, job_id=job["id"]
        )
        if capacity is not None:
            tx.execute(
                "UPDATE venue SET capacity = %s WHERE id = %s AND capacity IS NULL",
                (capacity, venue["id"]),
            )

    detail = (
        f"ground_venue: matched={matched} capacity={capacity or '-'}"
        + (
            f" evidence={evidence[:160]!r} source={source_url}"
            if evidence and source_url else ""
        )
    )
    tx.execute(
        "INSERT INTO crawl_log (job_id, finished_at, status, detail) "
        "VALUES (%s, now(), 'ok', %s)",
        (job["id"], detail[:2000]),
    )
    return []


HANDLERS = {
    "crawl": crawl, "resolve": resolve, "enrich": enrich,
    "embed_tags": embed_tags,
    "onboard": onboard, "agent_extract": agent_extract, "probe": probe,
    "discover": discover, "qa_check": qa_check,
    "verify_event": verify_event,
    "hydrate_event": hydrate_event,
    "ground_venue": ground_venue,
    "parity_audit": parity_audit,
}
