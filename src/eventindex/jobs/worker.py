"""The worker loop (H7): one process consuming the jobs table.

Every handler is a pure function (job, tx) -> [jobs to enqueue]; all writes go
through the transaction it receives. Handler success, its writes, and the
job's 'done' status commit atomically.
"""

import argparse
import logging
import time
import traceback

from eventindex import config, db
from eventindex.budget import (
    DailyBudgetExceeded,
    PAID_JOB_KINDS,
    ProviderUnavailable,
    RECOVERY_JOB_KINDS,
)
from eventindex.jobs.handlers import HANDLERS

log = logging.getLogger("eventindex.worker")


def enqueue(tx, kind: str, payload: dict | None = None, run_after=None) -> None:
    from psycopg.types.json import Jsonb

    tx.execute(
        "INSERT INTO jobs (kind, payload, run_after) "
        "VALUES (%s, %s, coalesce(%s, now()))",
        (kind, Jsonb(payload or {}), run_after),
    )


def requeue_stale(conn) -> int:
    """Return crashed-mid-run jobs to the queue (single worker: only relevant
    after an unclean shutdown)."""
    with conn.transaction():
        cur = conn.execute(
            "UPDATE jobs SET status = 'pending' "
            "WHERE status = 'running' AND started_at < now() - %s * interval '1 second'",
            (config.JOB_STALE_RUNNING_S,),
        )
        return cur.rowcount


def claim_next(conn) -> dict | None:
    with conn.transaction():
        # Serialize only the millisecond claim decision so concurrent worker
        # startups observe each other's committed lane occupancy.  Handlers
        # still run concurrently after this transaction releases the lock.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('eventindex.claim_next'))"
        )
        return conn.execute(
            """
            WITH next AS (
                SELECT id FROM jobs
                WHERE status = 'pending' AND run_after <= now()
                -- A durable account-wide breaker suppresses every job kind
                -- that may reach OpenRouter.  Deterministic embedding work
                -- remains runnable and one periodic free balance probe clears
                -- the row after a top-up.
                AND NOT (
                    kind = ANY(%s)
                    AND EXISTS (
                        SELECT 1 FROM provider_circuit
                        WHERE provider = 'openrouter'
                    )
                )
                -- Search readiness is derived data on already collected
                -- events. Drain it ahead of slower acquisition so a large
                -- crawl/hydration backlog cannot publish tagless events for
                -- days. This is kind-generic, never source/site-specific.
                ORDER BY CASE kind
                    -- Canon publication coalesces duplicate resolve requests;
                    -- one due atomic rebuild must not sit behind thousands of
                    -- derived jobs that only become useful after publication.
                    WHEN 'resolve' THEN 0
                    -- A monthly reset must let proven acquisition sources
                    -- refresh before staleness decay hides their otherwise
                    -- healthy events.  yield_ema is the existing generic
                    -- productivity signal; this never names a source/site.
                    WHEN 'crawl' THEN CASE WHEN EXISTS (
                        SELECT 1 FROM source s
                        WHERE s.id = (jobs.payload->>'source_id')::uuid
                          AND s.yield_ema >= %s
                    ) AND NOT EXISTS (
                        -- Reserve one acquisition lane while leaving the
                        -- other workers free to drain overdue hydration.
                        SELECT 1 FROM jobs active
                        WHERE active.kind = 'crawl'
                          AND active.status = 'running'
                    ) THEN 1 ELSE 8 END
                    -- Hydration has an explicit <24h publication SLA. A
                    -- schema-wide rebuild can enqueue thousands of enrich
                    -- jobs at once; fixed enrichment priority otherwise
                    -- starves already-overdue public fact recovery forever.
                    WHEN 'hydrate_event' THEN
                        CASE WHEN created_at < now() - interval '24 hours'
                                  AND NOT EXISTS (
                            -- One hydration lane satisfies the recovery SLA
                            -- without starving search-readiness enrichment.
                            SELECT 1 FROM jobs active
                            WHERE active.kind = 'hydrate_event'
                              AND active.status = 'running'
                        )
                             THEN 2 ELSE 7 END
                    WHEN 'enrich' THEN 3
                    WHEN 'embed_tags' THEN 4
                    -- A due resolve publishes recovered claims in batches.
                    WHEN 'verify_event' THEN 5
                    -- Grounding is admitted in small scheduler batches, so
                    -- giving it the first recovery slot cannot starve the
                    -- much larger hydration backlog; workers drain the batch
                    -- and spend the rest of the tick on event facts.
                    WHEN 'ground_venue' THEN 6
                    ELSE 8
                END,
                CASE WHEN kind = 'crawl' THEN (
                    SELECT -s.yield_ema FROM source s
                    WHERE s.id = (jobs.payload->>'source_id')::uuid
                ) END,
                -- Freshly changed canon should become fully searchable
                -- before old cache-miss backlog.  Within the same rebuild
                -- generation, next occurrence still gives deterministic
                -- near-term ordering below.
                CASE WHEN kind = 'enrich' THEN (
                    SELECT e.updated_at FROM event e
                    WHERE e.id = (jobs.payload->>'event_id')::uuid
                ) END DESC NULLS LAST,
                CASE WHEN kind = 'enrich' THEN
                    coalesce(
                        (payload->>'next_start')::timestamptz,
                        'infinity'::timestamptz
                    )
                END,
                run_after
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs j
            SET status = 'running', started_at = now(), attempts = attempts + 1
            FROM next WHERE j.id = next.id
            RETURNING j.*
            """,
            (list(PAID_JOB_KINDS), config.COMPLETENESS_MIN_YIELD),
        ).fetchone()


def _persist_agent_notes(conn, job: dict, exc: Exception) -> None:
    """A failed agent session's learnings (OnboardFailed.notes) must survive
    its rolled-back handler tx - same pattern as the error crawl_log. The
    next attempt's prompt starts from them instead of re-deriving the site."""
    from psycopg.types.json import Jsonb

    notes = getattr(exc, "notes", "")
    source_id = job.get("payload", {}).get("source_id")
    if not notes or not source_id:
        return
    with conn.transaction():
        row = conn.execute(
            "SELECT extraction_hint->'onboard_notes' AS notes FROM source "
            "WHERE id = %s", (source_id,),
        ).fetchone()
        if row is None:
            return
        merged = ([notes[:500]] + (row["notes"] or []))[:3]
        conn.execute(
            "UPDATE source SET extraction_hint = coalesce(extraction_hint, "
            "'{}'::jsonb) || jsonb_build_object('onboard_notes', %s::jsonb) "
            "WHERE id = %s",
            (Jsonb(merged), source_id),
        )


def run_job(conn, job: dict) -> None:
    try:
        with conn.transaction():
            new_jobs = HANDLERS[job["kind"]](job, conn)
            for nj in new_jobs:
                enqueue(conn, nj["kind"], nj.get("payload"), nj.get("run_after"))
            conn.execute(
                "UPDATE jobs SET status = 'done', finished_at = now() WHERE id = %s",
                (job["id"],),
            )
        log.info("job %s (%s) done, enqueued %d", job["id"], job["kind"], len(new_jobs))
    except Exception as exc:
        error = traceback.format_exc(limit=20)
        if isinstance(exc, DailyBudgetExceeded) or (
            "BudgetExceeded: global daily" in error
        ):
            # A lane cap parks the whole bulk lane; the global cap parks every
            # potentially paid kind.  Workers keep running deterministic work
            # instead of exiting into systemd's Restart=always loop.
            lane = getattr(exc, "lane", None)
            kinds = RECOVERY_JOB_KINDS if lane == "recovery" else PAID_JOB_KINDS
            last_error = (
                "recovery daily budget - waiting for reset"
                if lane == "recovery"
                else "global daily budget - waiting for reset"
            )
            with conn.transaction():
                conn.execute(
                    "UPDATE jobs SET "
                    "status = CASE WHEN id = %(id)s THEN 'pending' ELSE status END, "
                    "attempts = attempts - CASE WHEN id = %(id)s THEN 1 ELSE 0 END, "
                    "started_at = CASE WHEN id = %(id)s THEN NULL ELSE started_at END, "
                    "run_after = greatest(run_after, "
                    " (date_trunc('day', now() AT TIME ZONE %(tz)s) "
                    "  + interval '1 day 5 minutes') AT TIME ZONE %(tz)s), "
                    "last_error = %(last_error)s "
                    "WHERE id = %(id)s OR (kind = ANY(%(kinds)s) "
                    "AND status = 'pending')",
                    {
                        "id": job["id"],
                        "kinds": list(kinds),
                        "last_error": last_error,
                        "tz": config.TIMEZONE,
                    },
                )
            log.warning(
                "%s cap reached - job %s and lane siblings parked until midnight",
                lane or "global", job["id"],
            )
            return
        if "BudgetExceeded" in error and "monthly budget reached" in error:
            # per-source condition: park THIS job until the Vienna month
            # rolls over; other sources keep working, so no worker exit
            with conn.transaction():
                conn.execute(
                    "UPDATE jobs SET status = 'pending', attempts = attempts - 1, "
                    "run_after = (date_trunc('month', now() AT TIME ZONE %s) "
                    "  + interval '1 month 5 minutes') AT TIME ZONE %s, "
                    "last_error = 'source monthly budget - waiting for month rollover' "
                    "WHERE id = %s",
                    (config.TIMEZONE, config.TIMEZONE, job["id"]),
                )
            log.warning("monthly budget hit - job %s parked until next month", job["id"])
            return
        if isinstance(exc, ProviderUnavailable) or (
            "Insufficient credits" in error or "Error code: 402" in error
        ):
            # The durable provider_circuit row is checked before queue claims
            # and API calls. Park every potentially paid sibling, not merely
            # the current kind, and keep the worker alive.
            blocked_until = getattr(exc, "blocked_until", None)
            with conn.transaction():
                conn.execute(
                    "UPDATE jobs SET "
                    "status = CASE WHEN id = %(id)s THEN 'pending' ELSE status END, "
                    "attempts = attempts - CASE WHEN id = %(id)s THEN 1 ELSE 0 END, "
                    "started_at = CASE WHEN id = %(id)s THEN NULL ELSE started_at END, "
                    "run_after = greatest(run_after, coalesce(%(until)s, "
                    " now() + interval '1 hour')), "
                    "last_error = 'credits empty' "
                    "WHERE id = %(id)s OR (kind = ANY(%(kinds)s) "
                    "AND status = 'pending')",
                    {
                        "id": job["id"],
                        "kinds": list(PAID_JOB_KINDS),
                        "until": blocked_until,
                    },
                )
            log.warning(
                "OpenRouter unavailable - all paid job kinds paused globally"
            )
            return
        _persist_agent_notes(conn, job, exc)
        with conn.transaction():
            if job["attempts"] >= config.JOB_MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE jobs SET status = 'failed', finished_at = now(), "
                    "last_error = %s WHERE id = %s",
                    (error, job["id"]),
                )
                if job["kind"] == "onboard" and job["payload"].get("source_id"):
                    # a source whose onboarding definitively failed must not
                    # keep crawling in the hintless fallback mode it was
                    # escalated to escape (linztermine deep, 2026-07-09:
                    # 3 failed onboards, then daily homepage crawls yielding
                    # 5 events). degraded = out of crawl scheduling, visible
                    # in the digest's failed-jobs section.
                    conn.execute(
                        "UPDATE source SET status = 'degraded' "
                        "WHERE id = %s AND recipe IS NULL",
                        (job["payload"]["source_id"],),
                    )
            else:
                backoff = config.JOB_RETRY_BACKOFF_S * 5 ** (job["attempts"] - 1)
                conn.execute(
                    "UPDATE jobs SET status = 'pending', last_error = %s, "
                    "run_after = now() + %s * interval '1 second' WHERE id = %s",
                    (error, backoff, job["id"]),
                )
            if job["kind"] == "crawl" and job["payload"].get("source_id"):
                # the handler tx rolled back, taking its crawl_log with it -
                # without this row the scheduler re-enqueues a broken source
                # forever and park/escalation logic is blind to the failures
                conn.execute(
                    "INSERT INTO crawl_log (job_id, source_id, finished_at, "
                    "status, detail) VALUES (%s, %s, now(), 'error', %s)",
                    (job["id"], job["payload"]["source_id"], error[-400:]),
                )
        log.warning("job %s (%s) failed (attempt %d)", job["id"], job["kind"], job["attempts"])


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--once", action="store_true", help="drain ready jobs, then exit"
    )
    args = parser.parse_args()

    with db.connect() as conn:
        last_stale_check = 0.0
        last_provider_check = 0.0
        while True:
            # periodically, not just at startup: a job orphaned by a restart
            # that happens < JOB_STALE_RUNNING_S after it started would
            # otherwise stay 'running' for the whole life of this process
            # (bit us live: a resolve job zombied for 19h, 2026-07-09)
            if time.monotonic() - last_stale_check > 600:
                last_stale_check = time.monotonic()
                stale = requeue_stale(conn)
                if stale:
                    log.warning("requeued %d stale running jobs", stale)
            if time.monotonic() - last_provider_check > 60:
                last_provider_check = time.monotonic()
                from eventindex import llm

                try:
                    llm.ensure_openrouter_available()
                except ProviderUnavailable:
                    pass
            job = claim_next(conn)
            if job is not None:
                run_job(conn, job)
            elif args.once:
                return
            else:
                time.sleep(config.WORKER_IDLE_POLL_S)


if __name__ == "__main__":
    main()
