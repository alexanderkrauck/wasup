from psycopg.types.json import Jsonb

from eventindex.jobs.schedule import completeness_escalation, park_dormant, schedule


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


def test_yieldless_sources_park_dormant_with_monthly_pulse(conn):
    junk = _source(conn, "junk", "now() - interval '40 days'", yield_ema=0.0)
    for _ in range(5):
        conn.execute(
            "INSERT INTO crawl_log (source_id, status, events_found) "
            "VALUES (%s, 'ok', 0)", (junk,),
        )
    young = _source(conn, "young", "now() - interval '2 days'", yield_ema=0.0)
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
