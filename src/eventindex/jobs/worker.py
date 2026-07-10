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
        return conn.execute(
            """
            WITH next AS (
                SELECT id FROM jobs
                WHERE status = 'pending' AND run_after <= now()
                ORDER BY run_after
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE jobs j
            SET status = 'running', started_at = now(), attempts = attempts + 1
            FROM next WHERE j.id = next.id
            RETURNING j.*
            """
        ).fetchone()


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
    except Exception:
        error = traceback.format_exc(limit=20)
        if "BudgetExceeded: global daily cap" in error:
            # the daily envelope is spent - system condition, not job failure:
            # park the job until the Vienna midnight reset, no attempt burned
            with conn.transaction():
                conn.execute(
                    "UPDATE jobs SET status = 'pending', attempts = attempts - 1, "
                    "run_after = (date_trunc('day', now() AT TIME ZONE %s) "
                    "  + interval '1 day 5 minutes') AT TIME ZONE %s, "
                    "last_error = 'daily budget cap - waiting for reset' "
                    "WHERE id = %s",
                    (config.TIMEZONE, config.TIMEZONE, job["id"]),
                )
            log.warning("daily cap reached - job %s parked until midnight, worker exiting",
                        job["id"])
            raise SystemExit(0)
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
        if "Insufficient credits" in error or "Error code: 402" in error:
            # credit outage is a system condition, not a job failure: pause
            # the job an hour without burning an attempt (learned 2026-07-07:
            # an empty OpenRouter balance mass-failed 5k jobs overnight)
            with conn.transaction():
                conn.execute(
                    "UPDATE jobs SET status = 'pending', attempts = attempts - 1, "
                    "run_after = now() + interval '1 hour', last_error = 'credits empty' "
                    "WHERE id = %s",
                    (job["id"],),
                )
            log.warning("credits empty - job %s paused 1h, worker exiting", job["id"])
            raise SystemExit(0)
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
            job = claim_next(conn)
            if job is not None:
                run_job(conn, job)
            elif args.once:
                return
            else:
                time.sleep(config.WORKER_IDLE_POLL_S)


if __name__ == "__main__":
    main()
