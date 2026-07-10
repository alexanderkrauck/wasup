"""Nightly digest (H7.3): the whole observability stack.

Summarizes the last 24h of crawl_log, jobs, and budget_spend into a text file
under var/digests/. Includes the dead-man's switch: no successful crawl in 48h
puts a loud warning at the top. Run by cron nightly; runnable by hand anytime.
"""

from datetime import datetime, timedelta, timezone

from eventindex import config, db


def gather_stats(conn) -> dict:
    crawls = conn.execute(
        "SELECT status, count(*) AS n, sum(events_found) AS events FROM crawl_log "
        "WHERE started_at >= now() - interval '24 hours' GROUP BY status"
    ).fetchall()
    spend = conn.execute(
        "SELECT category, sum(amount_eur) AS eur, count(*) AS n FROM budget_spend "
        "WHERE spent_at >= now() - interval '24 hours' GROUP BY category"
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
    day_curve = conn.execute(
        "SELECT o.starts_at::date AS day, count(DISTINCT o.event_id) AS n "
        "FROM occurrence o WHERE o.status = 'scheduled' "
        "AND o.starts_at BETWEEN now() AND now() + interval '28 days' "
        "GROUP BY 1 ORDER BY 1"
    ).fetchall()
    return {
        "crawls": crawls,
        "spend": spend,
        "failed_jobs": failed_jobs,
        "last_success": last_success,
        "qa": qa,
        "limits_hit": limits_hit,
        "budget_parked": budget_parked,
        "day_curve": day_curve,
    }


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
