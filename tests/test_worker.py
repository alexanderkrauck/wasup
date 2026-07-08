from eventindex import config
from eventindex.budget import BudgetExceeded
from eventindex.jobs import handlers
from eventindex.jobs.worker import claim_next, enqueue, run_job


def test_claim_on_empty_queue(conn):
    assert claim_next(conn) is None


def test_success_marks_done_and_enqueues_returned_jobs(conn, monkeypatch):
    def ok_handler(job, tx):
        return [{"kind": "test_kind", "payload": {"child": True}}]

    monkeypatch.setitem(handlers.HANDLERS, "test_kind", ok_handler)
    with conn.transaction():
        enqueue(conn, "test_kind", {"child": False})

    job = claim_next(conn)
    run_job(conn, job)

    row = conn.execute("SELECT * FROM jobs WHERE id = %s", (job["id"],)).fetchone()
    assert row["status"] == "done"
    child = conn.execute(
        "SELECT * FROM jobs WHERE status = 'pending'"
    ).fetchone()
    assert child["payload"] == {"child": True}


def test_failure_retries_with_backoff_then_fails(conn, monkeypatch):
    def bad_handler(job, tx):
        raise RuntimeError("boom")

    monkeypatch.setitem(handlers.HANDLERS, "test_kind", bad_handler)
    with conn.transaction():
        enqueue(conn, "test_kind")

    for attempt in range(1, config.JOB_MAX_ATTEMPTS + 1):
        # make the job claimable regardless of backoff
        conn.execute("UPDATE jobs SET run_after = now()")
        conn.commit()
        job = claim_next(conn)
        assert job["attempts"] == attempt
        run_job(conn, job)

    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["status"] == "failed"
    assert "boom" in row["last_error"]


def test_monthly_budget_parks_job_until_month_rollover(conn, monkeypatch):
    def broke_handler(job, tx):
        raise BudgetExceeded("source x monthly budget reached: €1.0 >= €1.0")

    monkeypatch.setitem(handlers.HANDLERS, "test_kind", broke_handler)
    with conn.transaction():
        enqueue(conn, "test_kind")
    run_job(conn, claim_next(conn))

    row = conn.execute("SELECT * FROM jobs").fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0  # a broke source is not a failing job
    days_parked = conn.execute(
        "SELECT extract(epoch FROM run_after - now()) / 86400 AS d FROM jobs"
    ).fetchone()["d"]
    assert days_parked > 0.5  # waits for the month rollover, not a backoff


def test_failed_crawl_leaves_error_trace_for_the_scheduler(conn, monkeypatch):
    sid = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust) VALUES "
        "('broken', 'https://broken.example', 'website', 3, 0.65) RETURNING id"
    ).fetchone()["id"]

    def bad_crawl(job, tx):
        raise RuntimeError("DNS boom")

    monkeypatch.setitem(handlers.HANDLERS, "crawl", bad_crawl)
    with conn.transaction():
        enqueue(conn, "crawl", {"source_id": str(sid)})
    run_job(conn, claim_next(conn))

    # the handler tx rolled back, but the failure evidence must survive -
    # it is what park/escalation logic reads
    log = conn.execute(
        "SELECT status, source_id FROM crawl_log"
    ).fetchone()
    assert log["status"] == "error"
    assert log["source_id"] == sid


def test_failed_writes_roll_back_with_the_job(conn, monkeypatch):
    def dirty_handler(job, tx):
        tx.execute(
            "INSERT INTO crawl_log (status, detail) VALUES ('ok', 'should vanish')"
        )
        raise RuntimeError("boom")

    monkeypatch.setitem(handlers.HANDLERS, "test_kind", dirty_handler)
    with conn.transaction():
        enqueue(conn, "test_kind")

    run_job(conn, claim_next(conn))

    assert conn.execute("SELECT count(*) AS n FROM crawl_log").fetchone()["n"] == 0
