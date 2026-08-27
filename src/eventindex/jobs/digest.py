"""Nightly digest (H7.3): the whole observability stack.

Summarizes the last 24h of crawl_log, jobs, and budget_spend into a text file
under var/digests/. Includes the dead-man's switch: no successful crawl in 48h
puts a loud warning at the top. Run by cron nightly; runnable by hand anytime.
"""

from datetime import datetime, timedelta, timezone

from eventindex import config, db
from eventindex.api.mcp_usage import (
    purge_old_usage,
    render_usage_report,
    usage_report,
)


def gather_stats(conn) -> dict:
    purge_old_usage(conn)
    mcp_usage = usage_report(conn, days=7)
    crawls = conn.execute(
        "SELECT status, count(*) AS n, sum(events_found) AS events FROM crawl_log "
        "WHERE started_at >= now() - interval '24 hours' GROUP BY status"
    ).fetchall()
    spend = conn.execute(
        "SELECT category, sum(amount_eur) AS eur, count(*) AS n FROM budget_spend "
        "WHERE spent_at >= now() - interval '24 hours' GROUP BY category"
    ).fetchall()
    paid_budget = conn.execute(
        "SELECT lane, sum(amount_eur) AS actual_eur, "
        "sum(reserved_eur) AS reserved_eur, "
        "sum(amount_eur) FILTER (WHERE state='uncertain') AS uncertain_eur "
        "FROM budget_spend WHERE spent_at >= "
        "date_trunc('day', now() AT TIME ZONE %s) AT TIME ZONE %s "
        "GROUP BY lane ORDER BY lane",
        (config.TIMEZONE, config.TIMEZONE),
    ).fetchall()
    failed_jobs = conn.execute(
        "SELECT kind, count(*) AS n FROM jobs "
        "WHERE status = 'failed' AND finished_at >= now() - interval '24 hours' "
        "GROUP BY kind"
    ).fetchall()
    last_success = conn.execute(
        "SELECT max(started_at) AS ts FROM crawl_log WHERE status IN ('ok', 'unchanged')"
    ).fetchone()["ts"]
    qa = conn.execute(
        "SELECT detail FROM crawl_log WHERE detail LIKE 'qa:%' "
        "AND started_at >= now() - interval '24 hours' ORDER BY started_at"
    ).fetchall()
    parity = conn.execute(
        "SELECT s.name, cl.detail FROM crawl_log cl "
        "LEFT JOIN source s ON s.id = cl.source_id "
        "WHERE cl.detail LIKE 'parity%%' "
        "AND cl.started_at >= now() - interval '7 days' ORDER BY cl.started_at"
    ).fetchall()
    # productive sources that hit a hard limit: events were demonstrably
    # left behind (page/state caps) or the source is parked on budget -
    # either way the index is silently incomplete without a loud flag
    limits_hit = conn.execute(
        "SELECT s.name, cl.events_found, cl.detail FROM crawl_log cl "
        "JOIN source s ON s.id = cl.source_id "
        "WHERE cl.detail LIKE '%%LIMIT-TRUNCATED%%' AND cl.events_found > 0 "
        "AND cl.started_at >= now() - interval '24 hours' "
        "ORDER BY cl.events_found DESC"
    ).fetchall()
    budget_parked = conn.execute(
        "SELECT s.name, s.yield_ema, j.last_error FROM jobs j "
        "JOIN source s ON s.id = (j.payload->>'source_id')::uuid "
        "WHERE j.status = 'pending' AND j.last_error LIKE '%%budget%%' "
        "AND j.run_after > now() AND s.yield_ema > 0"
    ).fetchall()
    degraded_productive = conn.execute(
        "SELECT s.name, s.status, count(DISTINCT i.event_id) AS events "
        "FROM source s JOIN event_claim c ON c.source_id = s.id "
        "JOIN identity i ON i.fingerprint = c.fingerprint "
        "WHERE s.status IN ('degraded', 'dormant') "
        "GROUP BY 1, 2 HAVING count(DISTINCT i.event_id) > 0 "
        "ORDER BY 3 DESC"
    ).fetchall()
    day_curve = conn.execute(
        "SELECT o.starts_at::date AS day, count(DISTINCT o.event_id) AS n "
        "FROM occurrence o WHERE o.status = 'scheduled' "
        "AND o.starts_at BETWEEN now() AND now() + interval '28 days' "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    # credit outage froze the pipeline silently for 4 days (2026-07-16..19):
    # parked jobs + the account balance are now first-class digest signals
    credit_parked = conn.execute(
        "SELECT count(*) AS n, max(run_after) AS resume FROM jobs "
        "WHERE status = 'pending' AND last_error = 'credits empty' "
        "AND run_after > now()"
    ).fetchone()
    fetch_blocked = conn.execute(
        """
        SELECT s.name FROM source s
        WHERE s.status IN ('active', 'degraded') AND 3 = (
            SELECT count(*) FROM (
                SELECT cl.status, cl.detail FROM crawl_log cl
                WHERE cl.source_id = s.id
                ORDER BY cl.started_at DESC LIMIT 3
            ) recent
            WHERE recent.status = 'error' AND recent.detail ~* %s
        )
        """,
        (r"403|429|turnstile|captcha|cloudflare|just a moment",),
    ).fetchall()
    field_completeness = conn.execute(
        """
        WITH future AS (
            SELECT DISTINCT e.id, e.price_min, e.booking_url, e.inferred,
                   e.expected_attendance,
                   EXISTS (
                       SELECT 1 FROM identity i
                       JOIN event_claim c ON c.fingerprint = i.fingerprint
                       JOIN source s ON s.id = c.source_id
                       WHERE i.event_id = e.id AND s.kind <> 'internal'
                         AND nullif(c.raw_excerpt, '') IS NOT NULL
                   ) AS has_identity_evidence
            FROM event e
            JOIN occurrence o ON o.event_id = e.id
            WHERE o.status = 'scheduled'
              AND coalesce(o.ends_at, o.starts_at) >= now()
        )
        SELECT count(*) AS future_events,
               count(*) FILTER (WHERE price_min IS NOT NULL) AS stated_price,
               count(*) FILTER (
                   WHERE price_min IS NOT NULL
                      OR inferred->'price'->>'min' IS NOT NULL
               ) AS any_price,
               count(*) FILTER (
                   WHERE booking_url IS NOT NULL AND price_min IS NULL
               ) AS booking_without_stated_price,
               count(*) FILTER (
                   WHERE expected_attendance IS NOT NULL
               ) AS event_scale,
               count(*) FILTER (
                   WHERE has_identity_evidence
               ) AS identity_evidence
        FROM future
        """
    ).fetchone()
    hydration = conn.execute(
        """
        SELECT count(*) FILTER (
                   WHERE status IN ('pending', 'running')
               ) AS unresolved,
               min(created_at) FILTER (
                   WHERE status IN ('pending', 'running')
               ) AS oldest_unresolved,
               count(*) FILTER (
                   WHERE status = 'failed'
                     AND finished_at >= now() - interval '24 hours'
               ) AS failed_24h
        FROM jobs WHERE kind = 'hydrate_event'
        """
    ).fetchone()
    verification = conn.execute(
        """
        SELECT count(*) FILTER (
                   WHERE j.status IN ('pending', 'running')
               ) AS unresolved,
               min(j.created_at) FILTER (
                   WHERE j.status IN ('pending', 'running')
               ) AS oldest_unresolved,
               count(*) FILTER (
                   WHERE cl.detail LIKE 'verify_event:%%outcome=supported%%'
                     AND cl.started_at >= now() - interval '24 hours'
               ) AS supported_24h,
               count(*) FILTER (
                   WHERE cl.detail LIKE 'verify_event:%%outcome=contradicted%%'
                     AND cl.started_at >= now() - interval '24 hours'
               ) AS contradicted_24h,
               count(*) FILTER (
                   WHERE cl.detail LIKE 'verify_event:%%outcome=unverified%%'
                     AND cl.started_at >= now() - interval '24 hours'
               ) AS unverified_24h
        FROM jobs j
        LEFT JOIN crawl_log cl ON cl.job_id = j.id
        WHERE j.kind = 'verify_event'
        """
    ).fetchone()
    audience_readiness = conn.execute(
        """
        WITH future_pending AS (
            SELECT e.id, e.updated_at
            FROM event e
            JOIN occurrence o ON o.event_id = e.id
            WHERE o.status = 'pending_enrichment'
              AND coalesce(o.ends_at, o.starts_at) >= now()
            GROUP BY e.id, e.updated_at
        ),
        future_scheduled AS (
            SELECT DISTINCT e.id,
                   coalesce(
                       e.expected_gender_split BETWEEN 0 AND 1
                       AND e.expected_gender_split_confidence > 0
                       AND e.expected_gender_split_confidence <= 1,
                       false
                   ) AS gender_valid,
                   coalesce(
                       e.inferred->>'energy' IN ('low', 'medium', 'high')
                       AND e.inferred #>>
                           '{_audience_essentials,energy,value}' =
                           e.inferred->>'energy'
                       AND CASE WHEN jsonb_typeof(
                           e.inferred #>
                           '{_audience_essentials,energy,confidence}'
                       ) = 'number' THEN
                           (e.inferred #>>
                               '{_audience_essentials,energy,confidence}'
                           )::numeric > 0
                           AND (e.inferred #>>
                               '{_audience_essentials,energy,confidence}'
                           )::numeric <= 1
                       ELSE false END,
                       false
                   ) AS energy_valid,
                   coalesce(
                       jsonb_typeof(
                           e.inferred #> '{solo_friendly,value}'
                       ) = 'boolean'
                       AND CASE WHEN jsonb_typeof(
                           e.inferred #> '{solo_friendly,confidence}'
                       ) = 'number' THEN
                           (e.inferred #>>
                               '{solo_friendly,confidence}'
                           )::numeric > 0
                           AND (e.inferred #>>
                               '{solo_friendly,confidence}'
                           )::numeric <= 1
                       ELSE false END,
                       false
                   ) AS solo_valid
            FROM event e
            JOIN occurrence o ON o.event_id = e.id
            WHERE o.status = 'scheduled'
              AND coalesce(o.ends_at, o.starts_at) >= now()
        )
        SELECT
            (SELECT count(*) FROM future_pending) AS pending_events,
            (SELECT min(updated_at) FROM future_pending) AS oldest_pending,
            (SELECT count(*) FROM future_pending p
             WHERE NOT EXISTS (
                 SELECT 1 FROM jobs j
                 WHERE j.kind = 'estimate_audience'
                   AND j.status IN ('pending', 'running')
                   AND coalesce(
                       j.payload->'event_ids', '[]'::jsonb
                   ) ? p.id::text
             )) AS orphan_pending,
            (SELECT count(*) FROM future_scheduled
             WHERE NOT (gender_valid AND energy_valid AND solo_valid))
                AS scheduled_violations,
            (SELECT count(*) FROM future_scheduled WHERE NOT gender_valid)
                AS gender_violations,
            (SELECT count(*) FROM future_scheduled WHERE NOT energy_valid)
                AS energy_violations,
            (SELECT count(*) FROM future_scheduled WHERE NOT solo_valid)
                AS solo_violations,
            (SELECT count(*) FROM jobs
             WHERE kind = 'estimate_audience' AND status = 'failed'
               AND finished_at >= now() - interval '24 hours')
                AS failed_24h
        """
    ).fetchone()
    return {
        "credit_parked": credit_parked,
        "openrouter_balance_usd": openrouter_balance(),
        "fetch_blocked": fetch_blocked,
        "crawls": crawls,
        "spend": spend,
        "paid_budget": paid_budget,
        "failed_jobs": failed_jobs,
        "last_success": last_success,
        "qa": qa,
        "parity": parity,
        "limits_hit": limits_hit,
        "degraded_productive": degraded_productive,
        "budget_parked": budget_parked,
        "day_curve": day_curve,
        "field_completeness": field_completeness,
        "hydration": hydration,
        "verification": verification,
        "audience_readiness": audience_readiness,
        "mcp_usage": mcp_usage,
    }


def openrouter_balance() -> float | None:
    """Remaining USD credits; None when unknown (no key, network, schema)."""
    import httpx

    if not config.OPENROUTER_API_KEY:
        return None
    try:
        resp = httpx.get(
            "https://openrouter.ai/api/v1/credits", timeout=10,
            headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}"},
        )
        data = resp.json()["data"]
        return float(data["total_credits"]) - float(data["total_usage"])
    except Exception:
        return None


def day_curve_anomalies(day_curve: list[dict]) -> list[str]:
    """Days holding < 50% of their weekday's median event count: the
    signature of a capped feed the projection machinery didn't cover
    (incompleteness red team, 2026-07-10). Pure function for testability."""
    from statistics import median

    by_weekday: dict[int, list[int]] = {}
    for r in day_curve:
        by_weekday.setdefault(r["day"].weekday(), []).append(r["n"])
    flags = []
    for r in day_curve:
        med = median(by_weekday[r["day"].weekday()])
        if med >= 4 and r["n"] < med * 0.5:
            flags.append(f"{r['day']} ({r['day']:%a}): {r['n']} events, "
                         f"weekday median {med:.0f}")
    return flags


def render(stats: dict, now: datetime) -> str:
    lines = [f"eventindex digest - {now:%Y-%m-%d %H:%M} UTC", ""]

    paid = stats.get("paid_budget") or []
    actual = sum(float(row.get("actual_eur") or 0) for row in paid)
    reserved = sum(float(row.get("reserved_eur") or 0) for row in paid)
    lines.append(
        f"paid-provider budget today: €{actual:.4f} actual + "
        f"€{reserved:.4f} reserved / €{config.GLOBAL_DAILY_PAID_CAP_EUR:.2f}"
    )
    for row in paid:
        lines.append(
            f"  {row['lane']}: €{float(row.get('actual_eur') or 0):.4f} actual"
            f" + €{float(row.get('reserved_eur') or 0):.4f} reserved"
        )
    lines.append("")

    usage = stats.get("mcp_usage")
    if usage:
        lines.extend(render_usage_report(usage).rstrip().splitlines())
        lines.append("")

    last = stats["last_success"]
    if last is None or now - last > timedelta(hours=config.DEAD_MAN_HOURS):
        seen = f"{last:%Y-%m-%d %H:%M}" if last else "never"
        lines += [
            "!" * 60,
            f"!! DEAD MAN'S SWITCH: no successful crawl in {config.DEAD_MAN_HOURS}h "
            f"(last: {seen})",
            "!" * 60,
            "",
        ]

    parked = stats.get("credit_parked") or {}
    if parked.get("n"):
        lines += [
            "!" * 60,
            f"!! LLM CREDITS EMPTY: {parked['n']} jobs paused (resume attempt "
            f"{parked['resume']:%Y-%m-%d %H:%M}). TOP UP OPENROUTER.",
            "!" * 60,
            "",
        ]
    balance = stats.get("openrouter_balance_usd")
    if balance is not None and balance < config.CREDITS_WARN_USD:
        lines += [
            "!" * 60,
            f"!! OPENROUTER BALANCE LOW: ${balance:.2f} left "
            f"(warn threshold ${config.CREDITS_WARN_USD:.0f}) - top up before "
            "the pipeline freezes.",
            "!" * 60,
            "",
        ]

    if stats.get("fetch_blocked"):
        lines += ["!" * 60,
                  "!! FETCH-BLOCKED SUSPECTS (anti-bot walls: human-visible, "
                  "bot-refused)"]
        for r in stats["fetch_blocked"]:
            lines.append(f"!!  {r['name']}")
        lines += ["!" * 60, ""]

    if stats.get("limits_hit") or stats.get("budget_parked"):
        lines += ["!" * 60,
                  "!! LIMITS HIT ON PRODUCTIVE SOURCES - EVENTS ARE BEING MISSED"]
        for r in stats.get("limits_hit", []):
            lines.append(f"!!  {r['name']} ({r['events_found']} events indexed, "
                         f"more exist): {r['detail'][-120:]}")
        for r in stats.get("budget_parked", []):
            lines.append(f"!!  {r['name']} (yield_ema {r['yield_ema']:.0f}) "
                         f"parked: {r['last_error'][:80]}")
        lines += ["!" * 60, ""]

    if stats.get("degraded_productive"):
        lines += ["!" * 60,
                  "!! PRODUCTIVE SOURCES DEGRADED - THEIR EVENTS WILL GO STALE"]
        for r in stats["degraded_productive"]:
            lines.append(f"!!  {r['name']} ({r['status']}): "
                         f"{r['events']} events in canon")
        lines += ["!" * 60, ""]

    audience = stats.get("audience_readiness") or {}
    audience_pending = audience.get("pending_events") or 0
    audience_oldest = audience.get("oldest_pending")
    audience_orphans = audience.get("orphan_pending") or 0
    audience_scheduled = audience.get("scheduled_violations") or 0
    audience_failed = audience.get("failed_24h") or 0
    if (
        audience_pending
        or audience_orphans
        or audience_scheduled
        or audience_failed
    ):
        oldest_age = (
            f"; oldest {now - audience_oldest} ago"
            if audience_oldest is not None else ""
        )
        lines += [
            "!" * 60,
            "!! AUDIENCE READINESS ALERT - PUBLICATION GATE NEEDS ATTENTION",
            f"!!  pending (not public): {audience_pending}{oldest_age}",
            "!!  pending without active estimate_audience job: "
            f"{audience_orphans}",
            f"!!  SCHEDULED INVARIANT VIOLATIONS: {audience_scheduled} "
            f"(gender={audience.get('gender_violations') or 0}, "
            f"energy={audience.get('energy_violations') or 0}, "
            f"solo={audience.get('solo_violations') or 0})",
            "!!  failed estimate_audience jobs (24h): "
            f"{audience_failed}",
            "!" * 60,
            "",
        ]
    else:
        lines.append(
            "audience publication readiness: healthy "
            "(0 pending, 0 scheduled violations, 0 failed jobs in 24h)"
        )

    lines.append("crawls (24h):")
    if stats["crawls"]:
        for r in stats["crawls"]:
            lines.append(f"  {r['status']}: {r['n']} (events found: {r['events'] or 0})")
    else:
        lines.append("  none")

    lines.append("spend (24h):")
    if stats["spend"]:
        for r in stats["spend"]:
            lines.append(f"  {r['category']}: €{r['eur']:.4f} over {r['n']} calls")
    else:
        lines.append("  none")

    lines.append("failed jobs (24h):")
    if stats["failed_jobs"]:
        for r in stats["failed_jobs"]:
            lines.append(f"  {r['kind']}: {r['n']}")
    else:
        lines.append("  none")

    fields = stats.get("field_completeness") or {}
    total = fields.get("future_events") or 0
    lines.append("future-event field completeness:")
    if total:
        stated = fields.get("stated_price") or 0
        any_price = fields.get("any_price") or 0
        scale = fields.get("event_scale") or 0
        evidence = fields.get("identity_evidence") or 0
        lines += [
            f"  stated price: {stated}/{total} ({stated / total:.1%})",
            f"  any price (stated or estimated): {any_price}/{total} "
            f"({any_price / total:.1%})",
            f"  event scale estimate: {scale}/{total} ({scale / total:.1%})",
            f"  evidence-backed identity: {evidence}/{total} "
            f"({evidence / total:.1%})",
            "  booking URL without stated price: "
            f"{fields.get('booking_without_stated_price') or 0}",
        ]
    else:
        lines.append("  no future events")
    hydration = stats.get("hydration") or {}
    unresolved = hydration.get("unresolved") or 0
    oldest = hydration.get("oldest_unresolved")
    if (
        unresolved
        and oldest is not None
        and now - oldest > timedelta(hours=24)
    ):
        lines += [
            "!" * 60,
            f"!! FACT RECOVERY SLA BREACH: {unresolved} unresolved jobs; "
            f"oldest is {now - oldest} old (target <24h).",
            "!" * 60,
        ]
    age = (
        f", oldest {now - oldest} ago"
        if oldest is not None else ""
    )
    lines.append(
        f"  hydration jobs: {unresolved} unresolved{age}, "
        f"{hydration.get('failed_24h') or 0} failed in 24h"
    )
    verification = stats.get("verification") or {}
    verify_unresolved = verification.get("unresolved") or 0
    verify_oldest = verification.get("oldest_unresolved")
    verify_age = (
        f", oldest {now - verify_oldest} ago"
        if verify_oldest is not None else ""
    )
    lines.append(
        f"  risk verification: {verify_unresolved} unresolved{verify_age}; "
        f"24h supported={verification.get('supported_24h') or 0}, "
        f"contradicted={verification.get('contradicted_24h') or 0}, "
        f"unverified={verification.get('unverified_24h') or 0}"
    )

    anomalies = day_curve_anomalies(stats.get("day_curve", []))
    lines.append("day-curve anomalies (28d, capped-feed signature):")
    if anomalies:
        lines += [f"  {a}" for a in anomalies]
    else:
        lines.append("  none")

    lines.append("qa checks (24h):")
    if stats.get("qa"):
        for r in stats["qa"]:
            lines.append(f"  {r['detail']}")
    else:
        lines.append("  none - QA loop did not run")

    lines.append("human-parity audit (7d):")
    if stats.get("parity"):
        for r in stats["parity"]:
            name = f"{r['name']}: " if r.get("name") else ""
            lines.append(f"  {name}{r['detail']}")
    else:
        lines.append("  none - parity audit did not run this week")

    return "\n".join(lines) + "\n"


def main() -> None:
    now = datetime.now(timezone.utc)
    with db.connect() as conn:
        text = render(gather_stats(conn), now)
    config.DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    path = config.DIGEST_DIR / f"{now:%Y-%m-%d}.txt"
    path.write_text(text)
    print(text)
    print(f"written to {path}")


if __name__ == "__main__":
    main()
