from psycopg.types.json import Jsonb

from eventindex.jobs.schedule import (
    completeness_escalation, enqueue_nightly_qa, escalate_broken, park_dormant,
    schedule,
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
    onboard = conn.execute("SELECT payload FROM jobs WHERE kind = 'onboard'").fetchone()
    assert onboard["payload"]["source_id"] == str(sick)
    assert "self-heal" in onboard["payload"]["reason"]
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
