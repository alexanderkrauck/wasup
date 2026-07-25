from psycopg.types.json import Jsonb

from eventindex.jobs.schedule import (
    completeness_escalation, enqueue_enrichment, enqueue_nightly_qa,
    escalate_broken, park_dormant, schedule,
)


def _source(conn, name, last_crawled_sql, status="active", interval="1 day",
            yield_ema=1.0):
    return conn.execute(
        f"INSERT INTO source (name, url, kind, tier, trust, status, crawl_interval, "
        f"yield_ema, last_crawled) "
        f"VALUES (%s, %s, 'website', 2, 0.8, %s, %s, %s, {last_crawled_sql}) "
        "RETURNING id",
        (name, f"https://{name}.at", status, interval, yield_ema),
    ).fetchone()["id"]


def test_due_and_never_crawled_enqueued_once(conn):
    _source(conn, "due", "now() - interval '2 days'")
    _source(conn, "fresh", "now() - interval '1 hour'")
    _source(conn, "never", "NULL")
    _source(conn, "blocked", "NULL", status="blocked")
    conn.commit()

    assert schedule(conn) == 2  # due + never
    conn.commit()
    assert schedule(conn) == 0  # already queued -> no duplicates
    names = {
        r["name"] for r in conn.execute(
            "SELECT s.name FROM jobs j JOIN source s ON s.id::text = j.payload->>'source_id' "
            "WHERE j.kind = 'crawl'"
        )
    }
    assert names == {"due", "never"}


def test_capped_feed_gets_companion_site_and_agent_once(conn):
    conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, yield_ema, extraction_hint) "
        "VALUES ('CityFeed', 'https://feed.example.at/export.php', 'api', 1, 0.9, "
        "50, %s)", (Jsonb({"horizon_days": 7.0}),),
    )
    # productive website with a healthy horizon: must NOT escalate
    conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, yield_ema, extraction_hint) "
        "VALUES ('DeepSite', 'https://deep.example.at/', 'website', 2, 0.8, "
        "50, %s)", (Jsonb({"horizon_days": 45.0}),),
    )
    conn.commit()

    assert completeness_escalation(conn) == 1
    conn.commit()
    companion = conn.execute(
        "SELECT * FROM source WHERE discovered_via = 'completeness_escalation'"
    ).fetchone()
    assert companion["url"] == "https://feed.example.at/"
    assert companion["kind"] == "website"
    onboard = conn.execute(
        "SELECT payload FROM jobs WHERE kind = 'onboard'"
    ).fetchone()
    assert onboard["payload"]["source_id"] == str(companion["id"])
    assert "completeness" in onboard["payload"]["reason"]
    # one-shot: second run does nothing
    assert completeness_escalation(conn) == 0


def _log(conn, source_id, status, n=1):
    for _ in range(n):
        conn.execute(
            "INSERT INTO crawl_log (source_id, status) VALUES (%s, %s)",
            (source_id, status),
        )


def test_persistently_erroring_source_escalates_once(conn):
    sick = _source(conn, "sick", "now()")
    _log(conn, sick, "error", n=5)
    flaky = _source(conn, "flaky", "now()")
    _log(conn, flaky, "error", n=4)
    _log(conn, flaky, "ok")  # one success inside the window -> not broken
    conn.commit()

    assert escalate_broken(conn) == 1
    conn.commit()
    status = conn.execute(
        "SELECT status FROM source WHERE id = %s", (sick,)
    ).fetchone()["status"]
    assert status == "degraded"
    repair = conn.execute(
        "SELECT payload FROM jobs WHERE kind = 'agent_extract'"
    ).fetchone()
    assert repair["payload"]["source_id"] == str(sick)
    assert "self-heal" in repair["payload"]["reason"]
    assert escalate_broken(conn) == 0  # degraded sources are out of the loop


def test_dormant_source_reactivates_on_yield(conn):
    import uuid as _uuid

    from eventindex.jobs import handlers

    sid = _source(conn, "sleeper", "NULL", status="dormant")
    source = conn.execute("SELECT * FROM source WHERE id = %s", (sid,)).fetchone()
    payload = {"title": {"value": "Fest", "confidence": 0.9},
               "starts_at": {"value": "2099-07-20T19:00:00+02:00", "confidence": 0.9}}

    handlers._update_source_stats(conn, {"id": _uuid.uuid4()}, source, [payload], "rss")
    row = conn.execute("SELECT status FROM source WHERE id = %s", (sid,)).fetchone()
    assert row["status"] == "active"  # the pulse crawl found life

    conn.execute("UPDATE source SET status = 'dormant' WHERE id = %s", (sid,))
    handlers._update_source_stats(conn, {"id": _uuid.uuid4()}, source, [], "rss")
    row = conn.execute("SELECT status FROM source WHERE id = %s", (sid,)).fetchone()
    assert row["status"] == "dormant"  # still yieldless -> stays parked


def test_nightly_qa_enqueued_once_per_day(conn):
    assert enqueue_nightly_qa(conn) is True
    conn.commit()
    assert enqueue_nightly_qa(conn) is False
    n = conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE kind = 'qa_check'"
    ).fetchone()
    assert n["n"] == 1


def test_enrichment_sweep_repairs_every_stale_future_event_once(conn):
    import uuid

    from eventindex import enrich, tags

    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, confidence, status, inferred) "
        "VALUES (%s, 'one_off', 'Yogakurse', 0.8, 'confirmed', %s)",
        (
            event_id,
            Jsonb({
                "_enrichment": {"schema_version": enrich.SCHEMA_VERSION - 1},
                "newcomer_friendly": {
                    "value": None, "confidence": 0, "evidence": None,
                },
            }),
        ),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at, status) "
        "VALUES (%s, now() + interval '3 days', 'scheduled')",
        (event_id,),
    )
    conn.commit()

    assert enqueue_enrichment(conn) == 1
    assert enqueue_enrichment(conn) == 0

    conn.execute("DELETE FROM jobs WHERE kind = 'enrich'")
    conn.execute(
        "UPDATE event SET inferred = %s WHERE id = %s",
        (
            Jsonb({
                "_enrichment": {"schema_version": enrich.SCHEMA_VERSION},
                "newcomer_friendly": {
                    "value": True, "confidence": 0.2, "evidence": None,
                },
            }),
            event_id,
        ),
    )
    for index in range(enrich.MIN_INFERRED_TAGS):
        tags.upsert(conn, event_id, f"concept {index}", 0.2, "inferred")
    assert enqueue_enrichment(conn) == 0


def test_enrichment_sweep_repairs_cache_projection_divergence(conn):
    import uuid

    from eventindex import enrich, tags
    from eventindex.jobs.schedule import enqueue_enrichment

    event_id = uuid.uuid4()
    key = "cache-winner"
    cached = {
        "age_min": {"value": 18.0, "confidence": 0.35, "evidence": None},
        "age_max": {"value": 60.0, "confidence": 0.35, "evidence": None},
        "newcomer_friendly": {
            "value": True, "confidence": 0.35, "evidence": None,
        },
        "language": {"value": "de", "confidence": 0.35, "evidence": None},
        "tags": [
            {"name": f"concept {i}", "confidence": 0.2, "evidence": None}
            for i in range(enrich.MIN_INFERRED_TAGS)
        ],
    }
    conn.execute(
        "INSERT INTO enrichment (content_key, attributes) VALUES (%s, %s)",
        (key, Jsonb(cached)),
    )
    conn.execute(
        "INSERT INTO event (id, kind, title, confidence, status, inferred, "
        "expected_age_range) VALUES (%s, 'one_off', 'Divergent', 0.8, "
        "'confirmed', %s, int4range(18, 60, '[]'))",
        (
            event_id,
            Jsonb({
                "newcomer_friendly": {
                    "value": False, "confidence": 0.35, "evidence": None,
                },
                "language": cached["language"],
                "_enrichment": {
                    "schema_version": enrich.SCHEMA_VERSION,
                    "content_key": key,
                },
            }),
        ),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) "
        "VALUES (%s, now() + interval '3 days')",
        (event_id,),
    )
    for tag in cached["tags"]:
        tags.upsert(conn, event_id, tag["name"], tag["confidence"], "inferred")

    assert enqueue_enrichment(conn) == 1


def test_yieldless_sources_park_dormant_with_monthly_pulse(conn):
    junk = _source(conn, "junk", "now() - interval '40 days'", yield_ema=0.0)
    for _ in range(5):
        conn.execute(
            "INSERT INTO crawl_log (source_id, status, events_found) "
            "VALUES (%s, 'ok', 0)", (junk,),
        )
    _source(conn, "young", "now() - interval '2 days'", yield_ema=0.0)
    conn.commit()

    assert park_dormant(conn) == 1  # junk parks, young hasn't earned it yet
    conn.commit()
    row = conn.execute("SELECT status FROM source WHERE id = %s", (junk,)).fetchone()
    assert row["status"] == "dormant"

    # dormant + 40 days since crawl -> pulse check still schedules it
    n = schedule(conn)
    conn.commit()
    scheduled = {
        r["url"] for r in conn.execute(
            "SELECT s.url FROM jobs j JOIN source s ON s.id::text = j.payload->>'source_id'"
        )
    }
    assert "https://junk.at" in scheduled
    assert n == 2  # junk (pulse) + young (due)


def test_locationless_source_gets_venue_escalation_once(conn):
    """Venue contract (2026-07-14): a source whose future events carry
    neither venue nor detail URL is re-onboarded once with venue orders."""
    import uuid

    from eventindex.jobs.schedule import venue_escalation

    src = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust) VALUES "
        "('WKO-like', 'https://wko-like.at/veranstaltungen', 'website', 3, 0.65) "
        "RETURNING id"
    ).fetchone()["id"]
    for i in range(6):
        eid, fp = uuid.uuid4(), f"fp-{i}"
        conn.execute(
            "INSERT INTO event (id, kind, title, confidence, status, url) "
            "VALUES (%s, 'one_off', %s, 0.8, 'confirmed', "
            "'https://wko-like.at/veranstaltungen')",  # bare listing url only
            (eid, f"Workshop {i}"),
        )
        conn.execute(
            "INSERT INTO occurrence (event_id, starts_at, status) "
            "VALUES (%s, now() + interval '7 days', 'scheduled')", (eid,),
        )
        conn.execute("INSERT INTO identity (fingerprint, event_id) VALUES (%s, %s)",
                     (fp, eid))
        conn.execute(
            "INSERT INTO event_claim (source_id, fingerprint, payload) "
            "VALUES (%s, %s, '{}')", (src, fp),
        )
    conn.commit()

    assert venue_escalation(conn) == 1
    job = conn.execute("SELECT payload FROM jobs WHERE kind='onboard'").fetchone()
    assert job["payload"]["source_id"] == str(src)
    assert "venue" in job["payload"]["reason"]
    assert venue_escalation(conn) == 0  # one-shot


def test_hydration_covers_every_future_event_with_missing_public_facts(conn):
    import uuid

    from eventindex.jobs.schedule import enqueue_hydration

    eid = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, confidence, status, url) VALUES "
        "(%s, 'one_off', 'Ortlos', 0.8, 'confirmed', 'https://x.at/events/42')",
        (eid,),
    )
    # timed occurrence (time_unknown=false), but the event has no location
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at, status, time_unknown) "
        "VALUES (%s, now() + interval '3 days', 'scheduled', false)", (eid,),
    )
    conn.commit()

    assert enqueue_hydration(conn) == 1
    assert conn.execute(
        "SELECT payload->>'event_id' AS e FROM jobs WHERE kind='hydrate_event'"
    ).fetchone()["e"] == str(eid)
    assert enqueue_hydration(conn) == 0  # 30d recovery budget respected


def test_venue_grounding_queue_stays_small_for_fact_recovery(conn):
    import uuid

    from eventindex.jobs.schedule import (
        VENUE_GROUND_QUEUE_TARGET,
        enqueue_venue_grounding,
    )

    for number in range(VENUE_GROUND_QUEUE_TARGET + 2):
        venue_id = uuid.uuid4()
        event_id = uuid.uuid4()
        conn.execute(
            "INSERT INTO venue (id, name) VALUES (%s, %s)",
            (venue_id, f"Venue {number}"),
        )
        conn.execute(
            "INSERT INTO event "
            "(id, kind, title, confidence, status, venue_id) VALUES "
            "(%s, 'one_off', %s, 0.8, 'confirmed', %s)",
            (event_id, f"Event {number}", venue_id),
        )
        conn.execute(
            "INSERT INTO occurrence "
            "(event_id, starts_at, status, time_unknown) "
            "VALUES (%s, now() + interval '3 days', 'scheduled', false)",
            (event_id,),
        )
    conn.commit()

    assert enqueue_venue_grounding(conn) == VENUE_GROUND_QUEUE_TARGET
    assert enqueue_venue_grounding(conn) == 0
    assert conn.execute(
        "SELECT count(*) AS n FROM jobs WHERE kind = 'ground_venue' "
        "AND status IN ('pending', 'running')"
    ).fetchone()["n"] == VENUE_GROUND_QUEUE_TARGET


def test_agentic_sources_get_agent_sessions_not_crawls(conn):
    from eventindex.jobs.schedule import enqueue_agentic, schedule

    sid = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, extraction_hint) "
        "VALUES ('Poster Cafe', 'https://pc.at', 'website', 3, 0.5, "
        "'{\"mode\": \"agentic\"}') RETURNING id"
    ).fetchone()["id"]
    conn.commit()

    assert enqueue_agentic(conn) == 1
    conn.commit()
    jobs = conn.execute("SELECT kind, payload FROM jobs").fetchall()
    assert [j["kind"] for j in jobs] == ["agent_extract"]
    # a pending session blocks a duplicate, and the crawl path skips agentic
    assert enqueue_agentic(conn) == 0
    schedule(conn)
    conn.commit()
    crawls = conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'crawl' "
        "AND payload->>'source_id' = %s", (str(sid),)
    ).fetchall()
    assert crawls == []


def test_degraded_sources_retry_weekly_then_park_dormant(conn):
    from psycopg.types.json import Jsonb

    from eventindex.jobs.schedule import SELFHEAL_MAX_ATTEMPTS, retry_degraded

    fresh = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, status) VALUES "
        "('Broken', 'https://b.at', 'website', 3, 0.5, 'degraded') RETURNING id"
    ).fetchone()["id"]
    spent = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, status, "
        "extraction_hint) VALUES ('Hopeless', 'https://h.at', 'website', 3, "
        "0.5, 'degraded', %s) RETURNING id",
        (Jsonb({"selfheal_attempts": SELFHEAL_MAX_ATTEMPTS}),),
    ).fetchone()["id"]
    conn.commit()

    assert retry_degraded(conn) == 1
    conn.commit()
    job = conn.execute(
        "SELECT payload FROM jobs WHERE kind = 'agent_extract'"
    ).fetchone()
    assert job["payload"]["source_id"] == str(fresh)
    assert "degraded retry cadence" in job["payload"]["reason"]
    tries = conn.execute(
        "SELECT extraction_hint->>'selfheal_attempts' AS t FROM source "
        "WHERE id = %s", (fresh,)
    ).fetchone()["t"]
    assert tries == "1"
    # attempts exhausted -> dormant (the monthly pulse stays its way back)
    hopeless = conn.execute(
        "SELECT status FROM source WHERE id = %s", (spent,)
    ).fetchone()["status"]
    assert hopeless == "dormant"
    # the pending repair job blocks a duplicate on the next tick
    assert retry_degraded(conn) == 0


def test_legacy_onboarding_does_not_delay_first_agent_recovery(conn):
    """The recovery ladder supersedes legacy onboarding failures: only a
    recent attempt by the same agent_extract mechanism starts its cooldown."""
    from eventindex.jobs.schedule import retry_degraded

    legacy = _source(conn, "legacy-failure", "now()", status="degraded")
    current = _source(conn, "current-failure", "now()", status="degraded")
    conn.execute(
        "INSERT INTO jobs (kind, payload, status, finished_at) VALUES "
        "('onboard', %s, 'failed', now())",
        (Jsonb({"source_id": str(legacy)}),),
    )
    conn.execute(
        "INSERT INTO jobs (kind, payload, status, finished_at) VALUES "
        "('agent_extract', %s, 'failed', now())",
        (Jsonb({"source_id": str(current)}),),
    )
    conn.commit()

    assert retry_degraded(conn) == 1
    repair = conn.execute(
        "SELECT payload FROM jobs WHERE kind = 'agent_extract' "
        "AND status = 'pending'"
    ).fetchone()
    assert repair["payload"]["source_id"] == str(legacy)


def test_weekly_parity_audit_enqueues_once(conn):
    from eventindex.jobs.schedule import enqueue_weekly_parity

    assert enqueue_weekly_parity(conn) is True
    conn.commit()
    assert enqueue_weekly_parity(conn) is False
    jobs = conn.execute(
        "SELECT payload FROM jobs WHERE kind = 'parity_audit'"
    ).fetchall()
    assert len(jobs) == 1
