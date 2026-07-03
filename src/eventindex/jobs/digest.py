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
    return {
        "crawls": crawls,
        "spend": spend,
        "failed_jobs": failed_jobs,
        "last_success": last_success,
    }


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
