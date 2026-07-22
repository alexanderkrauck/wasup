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


def test_final_onboard_failure_degrades_recipeless_source(conn, monkeypatch):
    """A source whose onboarding definitively failed must leave the crawl
    rotation (it would otherwise keep crawling in the hintless fallback mode
    it was escalated to escape); a source that still has a working recipe
    keeps it."""
    def bad_onboard(job, tx):
        raise RuntimeError("onboarding ended without recipe (exhausted)")

    monkeypatch.setitem(handlers.HANDLERS, "onboard", bad_onboard)
    src = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust) VALUES ('deep', "
        "'https://x.at/', 'website', 2, 0.8) RETURNING id"
    ).fetchone()["id"]
    with conn.transaction():
        enqueue(conn, "onboard", {"source_id": str(src)})

    for _ in range(config.JOB_MAX_ATTEMPTS):
        conn.execute("UPDATE jobs SET run_after = now()")
        conn.commit()
        run_job(conn, claim_next(conn))

    assert conn.execute(
        "SELECT status FROM jobs WHERE kind = 'onboard'"
    ).fetchone()["status"] == "failed"
    assert conn.execute(
        "SELECT status FROM source WHERE id = %s", (src,)
    ).fetchone()["status"] == "degraded"


def test_every_handler_follows_the_worker_calling_convention():
    """run_job calls HANDLERS[kind](job, tx) - timefix shipped as
    (tx, job) and burned its whole job backlog before ever running."""
    import inspect

    for kind, handler in handlers.HANDLERS.items():
        params = list(inspect.signature(handler).parameters)
        assert params[:2] == ["job", "tx"], (
            f"handler {kind!r} has signature {params}, expected (job, tx)"
        )


def test_ghost_target_handlers_noop_and_survive_their_imports(conn):
    """Handlers with a missing-row no-op path must reach it: this executes
    their function-local imports, which a signature check cannot (timefix
    imported a `fetch` that never existed - found live 2026-07-13 after
    700 failed jobs)."""
    import uuid

    ghost = str(uuid.uuid4())
    for kind in ("enrich", "timefix"):
        job = {"id": uuid.uuid4(), "kind": kind, "payload": {"event_id": ghost}}
        assert handlers.HANDLERS[kind](job, conn) == []


def test_resolve_queues_every_pending_enrichment_and_tag_embedding(conn, monkeypatch):
    """A schema bump must not strand rows beyond an arbitrary first page."""
    import uuid

    pending = [uuid.uuid4() for _ in range(300)]
    monkeypatch.setattr(
        handlers,
        "rebuild",
        lambda tx: {
            "claims": 1, "events": 300, "occurrences": 300,
            "venues_created": 0, "enrich_pending": pending,
        },
    )
    job_id = conn.execute(
        "INSERT INTO jobs (kind) VALUES ('resolve') RETURNING id"
    ).fetchone()["id"]
    jobs = handlers.resolve({"id": job_id, "payload": {}}, conn)
    assert [job["kind"] for job in jobs].count("enrich") == 300
    assert jobs[-1] == {"kind": "embed_tags", "payload": {}}


def test_embed_tags_handler_fills_missing_names(conn, monkeypatch):
    import uuid

    from eventindex import embeddings, tags

    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, confidence, status) "
        "VALUES (%s, 'one_off', 'Salsa', 0.9, 'confirmed')",
        (event_id,),
    )
    tags.upsert(conn, event_id, "salsa dancing", 0.8, "inferred")
    captured = []
    monkeypatch.setattr(
        embeddings,
        "store_missing",
        lambda tx, names: captured.extend(names) or len(names),
    )
    assert handlers.embed_tags({"id": uuid.uuid4(), "payload": {}}, conn) == []
    assert captured == ["salsa dancing"]


def test_detail_claims_kept_for_time_or_missing_venue():
    payloads = [
        {"title": {"value": "A"}, "starts_at": {"value": "2030-01-01 19:00:00"}},
        {"title": {"value": "B"}, "starts_at": {"value": "2030-01-01 00:00:00"},
         "venue": {"value": "Palais Kaufmännischer Verein"}},
        {"title": {"value": "C"}, "starts_at": {"value": "2030-01-01"}},
    ]
    # event already has a location: only the truly timed claim is news
    kept = handlers._detail_claims_worth_keeping(payloads, needs_venue=False)
    assert [p["title"]["value"] for p in kept] == ["A"]
    # event has no location: the venue-bearing date-only claim is news too
    kept = handlers._detail_claims_worth_keeping(payloads, needs_venue=True)
    assert [p["title"]["value"] for p in kept] == ["A", "B"]
    # on-the-hour starts are real times, not midnight placeholders
    assert handlers._detail_claims_worth_keeping(
        [{"starts_at": {"value": "2030-01-01T20:00"}}], needs_venue=False)


def test_failed_agent_session_notes_survive_the_rollback(conn, monkeypatch):
    from eventindex.discovery.onboard import OnboardFailed

    sid = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, extraction_hint) "
        "VALUES ('X', 'https://x.at', 'website', 3, 0.5, "
        "'{\"onboard_notes\": [\"older note\"]}') RETURNING id"
    ).fetchone()["id"]

    def failing_onboard(job, tx):
        raise OnboardFailed("exhausted", notes="use the nexudus json api")

    monkeypatch.setitem(handlers.HANDLERS, "onboard", failing_onboard)
    with conn.transaction():
        enqueue(conn, "onboard", {"source_id": str(sid)})
    run_job(conn, claim_next(conn))

    notes = conn.execute(
        "SELECT extraction_hint->'onboard_notes' AS n FROM source WHERE id = %s",
        (sid,),
    ).fetchone()["n"]
    assert notes == ["use the nexudus json api", "older note"]
