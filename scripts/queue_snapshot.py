"""Queue observability with zero infrastructure: append one CSV line per
run (cron/timer every 10 min), read it back with --show.

Snapshot: uv run python scripts/queue_snapshot.py
Show:     uv run python scripts/queue_snapshot.py --show [hours]
"""

import csv
import sys
from datetime import datetime, timezone

from eventindex import config, db

OUT = config.VAR_DIR / "queue_stats.csv"
KINDS = ["crawl", "onboard", "enrich", "probe", "resolve", "discover", "qa_check"]
FIELDS = (["ts"]
          + [f"{k}_{c}" for k in KINDS for c in ("pend", "run")]
          + ["done_1h", "failed_1h", "oldest_ready_h", "spend_24h_eur"])


def snapshot() -> None:
    with db.connect() as conn:
        rows = {r["kind"]: r for r in conn.execute(
            "SELECT kind, count(*) FILTER (WHERE status='pending') AS pend, "
            "count(*) FILTER (WHERE status='running') AS run "
            "FROM jobs GROUP BY kind")}
        flow = conn.execute(
            "SELECT count(*) FILTER (WHERE status='done' AND finished_at > now() - interval '1 hour') AS done_1h, "
            "count(*) FILTER (WHERE status='failed' AND finished_at > now() - interval '1 hour') AS failed_1h, "
            "round(extract(epoch FROM now() - min(run_after) FILTER "
            "  (WHERE status='pending' AND run_after <= now())) / 3600) AS oldest_ready_h "
            "FROM jobs").fetchone()
        spend = conn.execute(
            "SELECT round(coalesce(sum(amount_eur), 0)::numeric, 2) AS eur "
            "FROM budget_spend WHERE spent_at > now() - interval '24 hours'"
        ).fetchone()
    line = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "done_1h": flow["done_1h"], "failed_1h": flow["failed_1h"],
            "oldest_ready_h": flow["oldest_ready_h"] or 0,
            "spend_24h_eur": spend["eur"]}
    for k in KINDS:
        r = rows.get(k)
        line[f"{k}_pend"] = r["pend"] if r else 0
        line[f"{k}_run"] = r["run"] if r else 0
    new = not OUT.exists()
    with OUT.open("a") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerow(line)


def show(hours: int) -> None:
    if not OUT.exists():
        print(f"no snapshots yet ({OUT})")
        return
    cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
    with OUT.open() as f:
        rows = [r for r in csv.DictReader(f)
                if datetime.strptime(r["ts"], "%Y-%m-%d %H:%M")
                .replace(tzinfo=timezone.utc).timestamp() >= cutoff]
    if not rows:
        print(f"no snapshots in the last {hours}h")
        return
    cols = ["ts"] + [c for c in FIELDS[1:]
                     if any(r[c] not in ("0", "0.00") for r in rows)]
    widths = {c: max(len(c), *(len(r[c]) for r in rows)) for c in cols}
    print("  ".join(c.ljust(widths[c]) for c in cols))
    for r in rows:
        print("  ".join(r[c].ljust(widths[c]) for c in cols))


if __name__ == "__main__":
    if "--show" in sys.argv:
        idx = sys.argv.index("--show")
        hours = int(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else 24
        show(hours)
    else:
        snapshot()
