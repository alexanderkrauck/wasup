from eventindex.jobs.schedule import schedule


def _source(conn, name, last_crawled_sql, status="active", interval="1 day"):
    return conn.execute(
        f"INSERT INTO source (name, url, kind, tier, trust, status, crawl_interval, "
        f"last_crawled) VALUES (%s, %s, 'website', 2, 0.8, %s, %s, {last_crawled_sql}) "
        "RETURNING id",
        (name, f"https://{name}.at", status, interval),
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
