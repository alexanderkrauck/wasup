"""Scheduler (§3.1 / §5b cost governance): cron runs this; it inserts crawl
jobs for due sources, most valuable first. The worker does the rest.

priority = value / cost:
    value  = yield_ema × uniqueness      (uniqueness: share of the source's
             events found on NO other source - long-tail gyms ≈ 1, portals low)
    cost   = cost_ema (EUR/crawl, floored)

Due = now > last_crawled + crawl_interval; the 72h event-proximity boost (H6)
forces at-least-daily crawls near occurrences. Junk dies economically (H4.2):
sources that stay yieldless after enough crawls park as dormant with a
monthly pulse check - never deleted.

Run: uv run python -m eventindex.jobs.schedule   (cron: every 15 min)
"""

from eventindex import db
from eventindex.jobs.worker import enqueue

DORMANT_MIN_CRAWLS = 5
DORMANT_YIELD_EMA = 0.05
MAX_ENQUEUE_PER_RUN = 100  # the envelope: least valuable work falls off first


def park_dormant(conn) -> int:
    with conn.transaction():
        cur = conn.execute(
            """
            UPDATE source SET status = 'dormant' WHERE status = 'active'
              AND yield_ema < %s
              AND (SELECT count(*) FROM crawl_log cl WHERE cl.source_id = source.id) >= %s
            """,
            (DORMANT_YIELD_EMA, DORMANT_MIN_CRAWLS),
        )
        return cur.rowcount


def schedule(conn) -> int:
    parked = park_dormant(conn)
    if parked:
        print(f"parked {parked} yieldless sources as dormant")
    rows = conn.execute(
        """
        WITH proximate AS (
            SELECT DISTINCT c.source_id FROM occurrence o
            JOIN identity i ON i.event_id = o.event_id
            JOIN event_claim c ON c.fingerprint = i.fingerprint
            WHERE o.starts_at BETWEEN now() AND now() + interval '72 hours'
              AND o.status = 'scheduled'
        ),
        uniqueness AS (
            SELECT c.source_id,
                   avg(CASE WHEN others.n = 1 THEN 1.0 ELSE 0.0 END) AS share
            FROM event_claim c
            JOIN identity i ON i.fingerprint = c.fingerprint
            JOIN (
                SELECT i2.event_id, count(DISTINCT c2.source_id) AS n
                FROM identity i2 JOIN event_claim c2 ON c2.fingerprint = i2.fingerprint
                GROUP BY i2.event_id
            ) others ON others.event_id = i.event_id
            GROUP BY c.source_id
        )
        SELECT s.id,
               (s.yield_ema * coalesce(u.share, 1.0))
                 / greatest(s.cost_ema, 0.001) AS priority
        FROM source s
        LEFT JOIN uniqueness u ON u.source_id = s.id
        WHERE (
            (
                s.status = 'active' AND (
                    s.last_crawled IS NULL
                    OR now() > s.last_crawled + s.crawl_interval
                    OR (s.id IN (SELECT source_id FROM proximate)
                        AND now() > s.last_crawled + interval '24 hours')
                )
            ) OR (
                -- dormant pulse check, monthly (§3: never fully deleted)
                s.status = 'dormant'
                AND (s.last_crawled IS NULL OR now() > s.last_crawled + interval '30 days')
            )
        )
        AND NOT EXISTS (
            SELECT 1 FROM jobs j
            WHERE j.kind = 'crawl' AND j.status IN ('pending', 'running')
              AND j.payload->>'source_id' = s.id::text
        )
        ORDER BY priority DESC
        LIMIT %s
        """,
        (MAX_ENQUEUE_PER_RUN,),
    ).fetchall()
    with conn.transaction():
        for r in rows:
            enqueue(conn, "crawl", {"source_id": str(r["id"])})
    return len(rows)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--discover", action="store_true",
                        help="also enqueue the weekly discovery sweeps")
    args = parser.parse_args()
    with db.connect() as conn:
        print(f"enqueued {schedule(conn)} crawl jobs")
        if args.discover:
            from eventindex import config

            channels = ["google_places", "osm", "backlinks"]
            if config.BRAVE_SEARCH_API_KEY:
                channels.append("search")
            with conn.transaction():
                for channel in channels:
                    enqueue(conn, "discover", {"channel": channel})
            print(f"enqueued {len(channels)} discovery sweeps")


if __name__ == "__main__":
    main()
