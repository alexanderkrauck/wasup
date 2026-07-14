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


def completeness_escalation(conn) -> int:
    """Completeness contract (2026-07-07): a productive source whose yield
    horizon never reaches past ~10 days is a capped feed (the linztermine XML
    is a hard 7-day window while the site publishes months ahead). Feeds get
    a companion WEBSITE source with the onboarding agent thrown at it;
    website sources get re-onboarded with depth orders. Once per source."""
    from urllib.parse import urlparse

    from eventindex import config

    rows = conn.execute(
        """
        SELECT id, name, url, kind FROM source
        WHERE status = 'active'
          AND yield_ema >= %s
          AND (extraction_hint->>'horizon_days')::float <= %s
          AND extraction_hint->>'completeness_escalated' IS NULL
        """,
        (config.COMPLETENESS_MIN_YIELD, config.HORIZON_CAPPED_DAYS),
    ).fetchall()
    escalated = 0
    for r in rows:
        with conn.transaction():
            reason = (f"completeness escalation: source is productive but its yield "
                      f"never reaches past {config.HORIZON_CAPPED_DAYS} days - the "
                      f"real site publishes further ahead; build a recipe that digs "
                      f"deep (calendar months, date ranges).")
            if r["kind"] in ("api", "ics", "rss"):
                host = urlparse(r["url"]).netloc
                site_url = f"https://{host}/"
                companion = conn.execute(
                    """
                    INSERT INTO source (name, url, kind, entity_type, tier, trust,
                                        monthly_budget_eur, discovered_via)
                    VALUES (%s, %s, 'website', 'portal', 2, 0.8, %s,
                            'completeness_escalation')
                    ON CONFLICT (url) DO NOTHING RETURNING id
                    """,
                    (f"{r['name']} (site, deep)", site_url,
                     config.MONTHLY_BUDGET_EUR_BY_TIER[2]),
                ).fetchone()
                target = companion["id"] if companion else None
            else:
                target = r["id"]
            if target is not None:
                enqueue(conn, "onboard",
                        {"source_id": str(target), "reason": reason})
                escalated += 1
            conn.execute(
                "UPDATE source SET extraction_hint = coalesce(extraction_hint,'{}'::jsonb) "
                "|| '{\"completeness_escalated\": true}'::jsonb WHERE id = %s",
                (r["id"],),
            )
    return escalated


BROKEN_STREAK = 5


def escalate_broken(conn) -> int:
    """A source whose last BROKEN_STREAK crawls all errored is structurally
    broken, not unlucky: mark it degraded and send the onboarding agent
    (same self-heal path recipes use). Degraded sources are not scheduled,
    which ends the retry-forever loop; a successful re-onboard flips them
    back to active."""
    rows = conn.execute(
        """
        SELECT s.id, s.name FROM source s
        WHERE s.status IN ('active', 'dormant')
          AND (SELECT count(*) FROM crawl_log cl
               WHERE cl.source_id = s.id) >= %(n)s
          AND NOT EXISTS (
              SELECT 1 FROM (
                  SELECT status FROM crawl_log cl
                  WHERE cl.source_id = s.id
                  ORDER BY started_at DESC LIMIT %(n)s
              ) recent WHERE recent.status != 'error'
          )
        """,
        {"n": BROKEN_STREAK},
    ).fetchall()
    for r in rows:
        with conn.transaction():
            conn.execute(
                "UPDATE source SET status = 'degraded' WHERE id = %s", (r["id"],)
            )
            enqueue(conn, "onboard", {
                "source_id": str(r["id"]),
                "reason": f"self-heal: last {BROKEN_STREAK} crawls all errored",
            })
    return len(rows)


def enqueue_nightly_qa(conn) -> bool:
    """One qa_check sample per Vienna day (§12: the QA loop is the
    highest-leverage investment - every aggregator rots without it)."""
    from eventindex import config

    exists = conn.execute(
        "SELECT 1 FROM jobs WHERE kind = 'qa_check' AND created_at >= "
        "date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s",
        (config.TIMEZONE, config.TIMEZONE),
    ).fetchone()
    if exists:
        return False
    with conn.transaction():
        enqueue(conn, "qa_check", {"sample": config.QA_NIGHTLY_SAMPLE})
    return True


TIMEFIX_BATCH = 40  # per tick; politeness comes from CRAWL_DELAY_S per fetch


def enqueue_timefix(conn) -> int:
    """Every future occurrence missing a start time (Alexander 2026-07-13,
    audit A4) OR whose event has no location at all (venue contract,
    Alexander 2026-07-14: anything findable without a login is ALWAYS
    extracted) earns one detail-page re-fetch per 30 days."""
    rows = conn.execute(
        """
        SELECT DISTINCT e.id FROM event e
        JOIN occurrence o ON o.event_id = e.id
        WHERE (o.time_unknown OR (e.venue_id IS NULL AND e.geo IS NULL))
          AND o.status = 'scheduled'
          AND o.starts_at >= now()
          AND e.url IS NOT NULL AND e.url !~ '^https?://[^/]+/?$'
          -- an event url identical to a registered source url is the
          -- LISTING page, not a detail page - re-fetching it per event
          -- burns an LLM extraction to learn nothing new (WKO class)
          AND NOT EXISTS (SELECT 1 FROM source s2 WHERE s2.url = e.url)
          AND NOT EXISTS (
            SELECT 1 FROM jobs j WHERE j.kind = 'timefix'
              AND j.payload->>'event_id' = e.id::text
              AND j.created_at > now() - interval '30 days')
        LIMIT %s
        """,
        (TIMEFIX_BATCH,),
    ).fetchall()
    for r in rows:
        enqueue(conn, "timefix", {"event_id": str(r["id"])})
    return len(rows)


VENUE_ESCALATION_MIN_EVENTS = 5


def venue_escalation(conn) -> int:
    """Venue completeness contract (Alexander 2026-07-14): a source whose
    crawled future events mostly carry neither a venue nor their own detail
    URL gives the pipeline nothing to recover a location from (the detail
    re-fetch needs deep links) - re-onboard it once with venue orders; the
    hard gate lives in recipe self-validation. Found via WKO: recipe
    scraped title+date off the listing, everything else sat on detail
    pages it never followed."""
    rows = conn.execute(
        """
        SELECT s.id, s.name FROM source s
        WHERE s.status = 'active'
          AND s.extraction_hint->>'venue_escalated' IS NULL
          AND (
            SELECT count(*) FILTER (
                     WHERE e.venue_id IS NULL AND e.geo IS NULL
                       AND (e.url IS NULL OR e.url ~ '^https?://[^/]+/?$'
                            OR EXISTS (SELECT 1 FROM source s2
                                       WHERE s2.url = e.url))
                   ) >= greatest(%(min)s, 0.5 * count(*))
            FROM (
                SELECT DISTINCT e2.id, e2.venue_id, e2.geo, e2.url
                FROM event e2
                JOIN identity i ON i.event_id = e2.id
                JOIN event_claim c ON c.fingerprint = i.fingerprint
                JOIN occurrence o ON o.event_id = e2.id
                WHERE c.source_id = s.id AND o.starts_at > now()
            ) e
          )
        """,
        {"min": VENUE_ESCALATION_MIN_EVENTS},
    ).fetchall()
    for r in rows:
        with conn.transaction():
            enqueue(conn, "onboard", {
                "source_id": str(r["id"]),
                "reason": ("venue completeness escalation: this source's events "
                           "arrive without venue/location AND without their own "
                           "detail URLs. Extract both for every event (set "
                           "follow_detail=true if the listing does not show "
                           "them) - anything findable without a login must be "
                           "extracted, however unstructured."),
            })
            conn.execute(
                "UPDATE source SET extraction_hint = coalesce(extraction_hint,'{}'::jsonb) "
                "|| '{\"venue_escalated\": true}'::jsonb WHERE id = %s",
                (r["id"],),
            )
    return len(rows)


def schedule(conn) -> int:
    flagged = completeness_escalation(conn)
    if flagged:
        print(f"completeness escalation: {flagged} capped sources sent to onboarding")
    venue_flagged = venue_escalation(conn)
    if venue_flagged:
        print(f"venue escalation: {venue_flagged} location-less sources sent to onboarding")
    parked = park_dormant(conn)
    if parked:
        print(f"parked {parked} yieldless sources as dormant")
    broken = escalate_broken(conn)
    if broken:
        print(f"escalated {broken} persistently erroring sources to re-onboarding")
    if enqueue_nightly_qa(conn):
        print("enqueued the daily qa_check sample")
    fixed = enqueue_timefix(conn)
    if fixed:
        print(f"enqueued {fixed} timefix detail fetches (date-only events)")
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
            channels = ["google_places", "osm", "backlinks", "search"]
            with conn.transaction():
                for channel in channels:
                    enqueue(conn, "discover", {"channel": channel})
            print(f"enqueued {len(channels)} discovery sweeps")


if __name__ == "__main__":
    main()
