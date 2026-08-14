import pytest

from eventindex import config
from eventindex.budget import (
    BudgetExceeded,
    DailyBudgetExceeded,
    ProviderUnavailable,
    trip_provider_circuit,
)
from eventindex.jobs import handlers
from eventindex.jobs.worker import claim_next, enqueue, run_job


def test_claim_on_empty_queue(conn):
    assert claim_next(conn) is None


def test_newly_changed_event_enrichment_outranks_old_backlog(conn):
    older = conn.execute(
        "INSERT INTO event (id, kind, title, updated_at) VALUES "
        "(gen_random_uuid(), 'event', 'older', now() - interval '1 day') "
        "RETURNING id"
    ).fetchone()["id"]
    newer = conn.execute(
        "INSERT INTO event (id, kind, title, updated_at) VALUES "
        "(gen_random_uuid(), 'event', 'newer', now()) RETURNING id"
    ).fetchone()["id"]
    conn.execute(
        "INSERT INTO jobs (kind, payload) VALUES "
        "('enrich', jsonb_build_object('event_id', %s::text, "
        "'next_start', '2026-01-01T00:00:00Z')), "
        "('enrich', jsonb_build_object('event_id', %s::text, "
        "'next_start', '2027-01-01T00:00:00Z'))",
        (str(older), str(newer)),
    )

    assert claim_next(conn)["payload"]["event_id"] == str(newer)


def test_audience_gate_runs_immediately_after_resolve(conn):
    conn.execute(
        "INSERT INTO jobs (kind, payload) VALUES "
        "('crawl', '{}'), "
        "('enrich', '{\"next_start\":\"2026-01-01T00:00:00Z\"}'), "
        "('estimate_audience', "
        " '{\"event_ids\":[],\"next_start\":\"2026-01-01T00:00:00Z\"}'), "
        "('resolve', '{}')"
    )

    assert claim_next(conn)["kind"] == "resolve"
    assert claim_next(conn)["kind"] == "estimate_audience"


def test_overdue_hydration_sla_outranks_schema_wide_enrichment(conn):
    productive_source = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, yield_ema) VALUES "
        "('productive', 'https://productive.example', 'website', 2, 0.8, 20) "
        "RETURNING id"
    ).fetchone()["id"]
    with conn.transaction():
        conn.execute(
            "INSERT INTO jobs (kind, payload, run_after, created_at) VALUES "
            "('resolve', '{}', now(), now()), "
            "('crawl', '{}', now() - interval '1 day', now()), "
            "('hydrate_event', '{\"row\":1}', now() - interval '3 days', "
            " now() - interval '3 days'), "
            "('hydrate_event', '{\"row\":2}', now() - interval '3 days', "
            " now() - interval '3 days'), "
            "('embed_tags', '{}', now() - interval '2 days', now()), "
            "('enrich', '{\"next_start\":\"2099-02-01T00:00:00Z\"}', now(), now()), "
            "('enrich', '{\"next_start\":\"2099-01-01T00:00:00Z\"}', now(), now())"
        )
        conn.execute(
            "INSERT INTO jobs (kind, payload, run_after, created_at) VALUES "
            "('crawl', jsonb_build_object('source_id', %s::text), now(), now()), "
            "('crawl', jsonb_build_object('source_id', %s::text), now(), now())",
            (str(productive_source), str(productive_source)),
        )

    assert claim_next(conn)["kind"] == "resolve"
    productive = claim_next(conn)
    assert productive["kind"] == "crawl"
    assert productive["payload"]["source_id"] == str(productive_source)
    assert claim_next(conn)["kind"] == "hydrate_event"
    assert claim_next(conn)["kind"] == "enrich"
    claimed = claim_next(conn)
    assert claimed["kind"] == "enrich"
    assert claimed["payload"]["next_start"].startswith("2099-02")
    assert claim_next(conn)["kind"] == "embed_tags"
    assert claim_next(conn)["kind"] == "hydrate_event"
    assert claim_next(conn)["kind"] == "crawl"
    conn.execute(
        "UPDATE jobs SET status='done' WHERE id=%s", (productive["id"],)
    )
    assert claim_next(conn)["kind"] == "crawl"


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


def test_credit_outage_pauses_ready_siblings_without_burning_attempts(
    conn, monkeypatch,
):
    def broke_handler(job, tx):
        blocked = trip_provider_circuit("openrouter", "test outage")
        raise ProviderUnavailable(
            "Error code: 402 - OpenRouter credits empty",
            provider="openrouter",
            blocked_until=blocked,
        )

    monkeypatch.setitem(handlers.HANDLERS, "crawl", broke_handler)
    monkeypatch.setitem(handlers.HANDLERS, "enrich", broke_handler)
    with conn.transaction():
        enqueue(conn, "crawl", {"row": 1})
        enqueue(conn, "enrich", {"row": 2})
    job = claim_next(conn)
    run_job(conn, job)

    rows = conn.execute(
        "SELECT status, attempts, last_error, "
        "extract(epoch FROM run_after - now()) AS wait_s "
        "FROM jobs ORDER BY payload->>'row'"
    ).fetchall()
    assert [row["status"] for row in rows] == ["pending", "pending"]
    assert [row["attempts"] for row in rows] == [0, 0]
    assert all(row["last_error"] == "credits empty" for row in rows)
    assert all(row["wait_s"] > 3500 for row in rows)


def test_recovery_daily_cap_parks_recovery_but_leaves_crawl_ready(
    conn, monkeypatch,
):
    def capped(job, tx):
        raise DailyBudgetExceeded("recovery cap", lane="recovery")

    monkeypatch.setitem(handlers.HANDLERS, "hydrate_event", capped)
    with conn.transaction():
        enqueue(conn, "hydrate_event", {"row": 1})
        enqueue(conn, "ground_venue", {"row": 2})
        enqueue(conn, "crawl", {"row": 3})
    hydrate = conn.execute(
        "UPDATE jobs SET created_at=now()-interval '2 days', "
        "run_after=now()-interval '1 day' "
        "WHERE kind='hydrate_event' RETURNING id"
    ).fetchone()
    conn.commit()
    job = claim_next(conn)
    assert job["id"] == hydrate["id"]
    run_job(conn, job)

    recovery = conn.execute(
        "SELECT attempts, last_error, run_after > now() AS parked FROM jobs "
        "WHERE kind IN ('hydrate_event','ground_venue') ORDER BY kind"
    ).fetchall()
    assert all(row["attempts"] == 0 for row in recovery)
    assert all(row["parked"] for row in recovery)
    assert all("recovery daily budget" in row["last_error"] for row in recovery)
    assert claim_next(conn)["kind"] == "crawl"


def test_open_provider_circuit_skips_paid_jobs(conn):
    trip_provider_circuit("openrouter", "empty")
    with conn.transaction():
        enqueue(conn, "crawl", {"row": 1})
        enqueue(conn, "embed_tags", {"row": 2})
    assert claim_next(conn)["kind"] == "embed_tags"
    assert claim_next(conn) is None


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
    """run_job calls HANDLERS[kind](job, tx) - a detail handler once shipped as
    (tx, job) and burned its whole job backlog before ever running."""
    import inspect

    for kind, handler in handlers.HANDLERS.items():
        params = list(inspect.signature(handler).parameters)
        assert params[:2] == ["job", "tx"], (
            f"handler {kind!r} has signature {params}, expected (job, tx)"
        )


def test_ghost_target_handlers_noop_and_survive_their_imports(conn):
    """Handlers with a missing-row no-op path must reach it: this executes
    their function-local imports, which a signature check cannot (the old detail
    imported a `fetch` that never existed - found live 2026-07-13 after
    700 failed jobs)."""
    import uuid

    ghost = str(uuid.uuid4())
    for kind in ("enrich", "hydrate_event"):
        job = {"id": uuid.uuid4(), "kind": kind, "payload": {"event_id": ghost}}
        assert handlers.HANDLERS[kind](job, conn) == []
    audience_job = {
        "id": uuid.uuid4(),
        "kind": "estimate_audience",
        "payload": {"event_ids": [ghost]},
    }
    assert handlers.estimate_audience(audience_job, conn) == []


def test_audience_handler_releases_event_before_full_enrichment(
    conn, monkeypatch,
):
    import uuid

    from eventindex import enrich as audience

    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, description, category) "
        "VALUES (%s, 'event', 'Salsaabend', 'Tanze mit uns', '{nightlife}')",
        (event_id,),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at, status) "
        "VALUES (%s, now() + interval '1 day', 'pending_enrichment')",
        (event_id,),
    )
    attrs = {
        "gender_split": {"value": 0.6, "confidence": 0.3, "evidence": None},
        "energy": {"value": "high", "confidence": 0.3, "evidence": None},
        "solo_friendly": {
            "value": True, "confidence": 0.2, "evidence": None,
        },
    }
    key = audience.audience_essentials_content_key({
        "title": "Salsaabend",
        "description": "Tanze mit uns",
        "category": ["nightlife"],
        "venue_name": None,
    })
    monkeypatch.setattr(
        audience,
        "estimate_audience_essentials",
        lambda tx, rows, job_id=None: {str(event_id): (key, attrs)},
    )

    jobs = handlers.estimate_audience(
        {
            "id": uuid.uuid4(),
            "kind": "estimate_audience",
            "payload": {"event_ids": [str(event_id)]},
        },
        conn,
    )

    row = conn.execute(
        "SELECT expected_gender_split, inferred FROM event WHERE id = %s",
        (event_id,),
    ).fetchone()
    assert row["expected_gender_split"] == 0.6
    assert row["inferred"]["energy"] == "high"
    assert row["inferred"]["solo_friendly"]["value"] is True
    assert conn.execute(
        "SELECT status FROM occurrence WHERE event_id = %s", (event_id,)
    ).fetchone()["status"] == "scheduled"
    assert jobs == [{
        "kind": "enrich",
        "payload": {
            "event_id": str(event_id),
            "next_start": conn.execute(
                "SELECT min(starts_at) AS starts FROM occurrence "
                "WHERE event_id = %s",
                (event_id,),
            ).fetchone()["starts"].isoformat(),
        },
    }]


def test_audience_handler_rechecks_content_after_model_call(conn, monkeypatch):
    import uuid

    from eventindex import enrich as audience

    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, description, category) "
        "VALUES (%s, 'event', 'Alter Titel', 'Alter Text', '{culture}')",
        (event_id,),
    )
    starts = conn.execute(
        "INSERT INTO occurrence (event_id, starts_at, status) "
        "VALUES (%s, now() + interval '1 day', 'pending_enrichment') "
        "RETURNING starts_at",
        (event_id,),
    ).fetchone()["starts_at"]
    old = {
        "id": event_id,
        "title": "Alter Titel",
        "description": "Alter Text",
        "category": ["culture"],
        "venue_name": None,
    }
    attrs = {
        "gender_split": {"value": 0.5, "confidence": 0.2, "evidence": None},
        "energy": {"value": "low", "confidence": 0.2, "evidence": None},
        "solo_friendly": {"value": True, "confidence": 0.2, "evidence": None},
    }

    def estimate(tx, rows, job_id=None):
        tx.execute(
            "UPDATE event SET title = 'Neuer Titel' WHERE id = %s",
            (event_id,),
        )
        return {
            str(event_id): (
                audience.audience_essentials_content_key(old), attrs,
            ),
        }

    monkeypatch.setattr(audience, "estimate_audience_essentials", estimate)
    jobs = handlers.estimate_audience(
        {
            "id": uuid.uuid4(),
            "kind": "estimate_audience",
            "payload": {"event_ids": [str(event_id)]},
        },
        conn,
    )

    row = conn.execute(
        "SELECT expected_gender_split FROM event WHERE id = %s",
        (event_id,),
    ).fetchone()
    assert row["expected_gender_split"] is None
    assert conn.execute(
        "SELECT status FROM occurrence WHERE event_id = %s", (event_id,),
    ).fetchone()["status"] == "pending_enrichment"
    assert jobs == [{
        "kind": "estimate_audience",
        "payload": {
            "event_ids": [str(event_id)],
            "next_start": starts.isoformat(),
        },
    }]


def test_audience_handler_seeds_confidence_from_full_cache(conn):
    import uuid

    from psycopg.types.json import Jsonb

    from eventindex import enrich as audience

    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, description, category) "
        "VALUES (%s, 'event', 'Lesung', 'Autor liest', '{culture}')",
        (event_id,),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at, status) "
        "VALUES (%s, now() + interval '1 day', 'pending_enrichment')",
        (event_id,),
    )
    canonical = {
        "id": event_id,
        "title": "Lesung",
        "description": "Autor liest",
        "category": ["culture"],
        "venue_name": None,
        "price_min": None,
        "price_max": None,
    }
    full = {
        "gender_split": {
            "value": 0.55, "confidence": 0.3, "evidence": None,
        },
        "energy": "low",
        "solo_friendly": {
            "value": True, "confidence": 0.3, "evidence": None,
        },
    }
    conn.execute(
        "INSERT INTO enrichment (content_key, attributes, model) "
        "VALUES (%s, %s, 'legacy-full')",
        (audience.content_key(canonical), Jsonb(full)),
    )

    jobs = handlers.estimate_audience(
        {
            "id": uuid.uuid4(),
            "kind": "estimate_audience",
            "payload": {"event_ids": [str(event_id)]},
        },
        conn,
    )

    row = conn.execute(
        "SELECT expected_gender_split, inferred FROM event WHERE id = %s",
        (event_id,),
    ).fetchone()
    assert row["expected_gender_split"] == 0.55
    assert row["inferred"]["energy"] == "low"
    assert row["inferred"]["_audience_essentials"]["energy"] == {
        "value": "low", "confidence": 0.2, "evidence": None,
    }
    assert conn.execute(
        "SELECT status FROM occurrence WHERE event_id = %s", (event_id,),
    ).fetchone()["status"] == "scheduled"
    assert jobs == []


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


def test_resolve_batches_publication_gated_events_before_full_enrichment(
    conn, monkeypatch,
):
    import uuid

    pending = []
    for index in range(41):
        event_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO event (id, kind, title) VALUES (%s, 'event', %s)",
            (event_id, f"Pending {index}"),
        )
        pending.append(event_id)
        conn.execute(
            "INSERT INTO occurrence (event_id, starts_at, status) "
            "VALUES (%s, now() + interval '1 day', 'pending_enrichment')",
            (event_id,),
        )
    monkeypatch.setattr(
        handlers,
        "rebuild",
        lambda tx: {
            "claims": 1,
            "events": len(pending),
            "occurrences": len(pending),
            "venues_created": 0,
            "enrich_pending": pending,
        },
    )
    job_id = conn.execute(
        "INSERT INTO jobs (kind) VALUES ('resolve') RETURNING id"
    ).fetchone()["id"]

    jobs = handlers.resolve({"id": job_id, "payload": {}}, conn)
    audience_jobs = [job for job in jobs if job["kind"] == "estimate_audience"]

    assert [len(job["payload"]["event_ids"]) for job in audience_jobs] == [20, 20, 1]
    assert {
        event_id
        for job in audience_jobs for event_id in job["payload"]["event_ids"]
    } == {str(event_id) for event_id in pending}
    assert not [job for job in jobs if job["kind"] == "enrich"]


def test_lock_losing_resolve_reuses_existing_pending_followup(conn, monkeypatch):
    monkeypatch.setattr(
        handlers, "rebuild", lambda tx: {"skipped": "another rebuild is running"}
    )
    conn.execute("INSERT INTO jobs (kind) VALUES ('resolve')")
    job_id = conn.execute(
        "INSERT INTO jobs (kind, status) VALUES ('resolve', 'running') RETURNING id"
    ).fetchone()["id"]

    assert handlers.resolve({"id": job_id, "payload": {}}, conn) == []
    assert conn.execute(
        "SELECT count(*) n FROM jobs WHERE kind='resolve' AND status='pending'"
    ).fetchone()["n"] == 1


def test_successful_resolve_collapses_pending_fanout_to_one(conn, monkeypatch):
    monkeypatch.setattr(
        handlers,
        "rebuild",
        lambda tx: {
            "claims": 1, "events": 1, "occurrences": 1,
            "venues_created": 0, "enrich_pending": [],
        },
    )
    conn.execute(
        "INSERT INTO jobs (kind) VALUES ('resolve'), ('resolve'), ('resolve')"
    )
    job_id = conn.execute(
        "INSERT INTO jobs (kind, status) VALUES ('resolve', 'running') RETURNING id"
    ).fetchone()["id"]

    handlers.resolve({"id": job_id, "payload": {}}, conn)

    assert conn.execute(
        "SELECT count(*) n FROM jobs WHERE kind='resolve' AND status='pending'"
    ).fetchone()["n"] == 1
    superseded = conn.execute(
        "SELECT count(*) n FROM jobs WHERE kind='resolve' AND status='done' "
        "AND last_error LIKE 'superseded by atomic rebuild %'"
    ).fetchone()["n"]
    assert superseded == 2


def test_followup_resolve_does_not_duplicate_enrichment_jobs(conn, monkeypatch):
    """Crawls may correctly queue a follow-up while a rebuild is running; its
    cache misses must not duplicate the first rebuild's still-pending work."""
    import uuid

    from psycopg.types.json import Jsonb

    pending = [uuid.uuid4(), uuid.uuid4()]
    monkeypatch.setattr(
        handlers,
        "rebuild",
        lambda tx: {
            "claims": 1, "events": 2, "occurrences": 2,
            "venues_created": 0, "enrich_pending": pending,
        },
    )
    conn.execute(
        "INSERT INTO jobs (kind, payload) VALUES ('enrich', %s)",
        (Jsonb({"event_id": str(pending[0])}),),
    )
    job_id = conn.execute(
        "INSERT INTO jobs (kind) VALUES ('resolve') RETURNING id"
    ).fetchone()["id"]
    jobs = handlers.resolve({"id": job_id, "payload": {}}, conn)
    assert [
        job["payload"]["event_id"] for job in jobs if job["kind"] == "enrich"
    ] == [str(pending[1])]


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
