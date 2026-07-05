"""Scheduler (§3.1 / H7): cron runs this; it inserts crawl jobs for due
sources. The worker does the rest. Boring by design.

Due = now > last_crawled + crawl_interval (never-crawled counts as due).
Skips sources that are blocked/dead or already queued. The 72h event-
proximity boost (H6): any source with an occurrence in the next 72h is
crawled at least daily regardless of its interval.

Run: uv run python -m eventindex.jobs.schedule   (cron: every 15 min)
"""

from eventindex import db
from eventindex.jobs.worker import enqueue


def schedule(conn) -> int:
    rows = conn.execute(
        """
        WITH proximate AS (          -- sources feeding an occurrence within 72h
            SELECT DISTINCT c.source_id FROM occurrence o
            JOIN identity i ON i.event_id = o.event_id
            JOIN event_claim c ON c.fingerprint = i.fingerprint
            WHERE o.starts_at BETWEEN now() AND now() + interval '72 hours'
              AND o.status = 'scheduled'
        )
        SELECT s.id FROM source s
        WHERE s.status = 'active'
          AND (
            s.last_crawled IS NULL
            OR now() > s.last_crawled + s.crawl_interval
            OR (s.id IN (SELECT source_id FROM proximate)
                AND now() > s.last_crawled + interval '24 hours')
          )
          AND NOT EXISTS (
            SELECT 1 FROM jobs j
            WHERE j.kind = 'crawl'
              AND j.status IN ('pending', 'running')
              AND j.payload->>'source_id' = s.id::text
          )
        """
    ).fetchall()
    with conn.transaction():
        for r in rows:
            enqueue(conn, "crawl", {"source_id": str(r["id"])})
    return len(rows)


def main() -> None:
    with db.connect() as conn:
        print(f"enqueued {schedule(conn)} crawl jobs")


if __name__ == "__main__":
    main()
