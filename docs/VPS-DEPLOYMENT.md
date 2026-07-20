# VPS Deployment Plan

Goal: the index runs 24/7 on a EU server with a domain + TLS, surviving
laptop sleep, presentable to authorities, and ready for the ChatGPT/MCP
surface (which requires public HTTPS). DECISIONS.md already locks the shape:
one small VPS, systemd services, Postgres on the same box, no docker needed.

## 0. Decisions Alexander must make (blockers)

- **Domain** (OPEN-QUESTIONS #7) — decided: `wasup.at`, product name
  "Wasup" (DECISIONS 2026-07-09; registration/A records Alexander's side).
  Needed for TLS, .ics subscriptions, the ChatGPT app, and every pitch deck.
- **Git remote** (OPEN-QUESTIONS #6 deferred it): deploys want `git pull`.
  Private GitHub repo is the boring default.

## 1. Provision (Hetzner, ~30 min)

- **CPX31 or CX32** (4 vCPU / 8 GB, ~EUR 8-15/mo) in Falkenstein or
  Nuremberg (EU = the GDPR answer). 8 GB because headless Chromium for
  onboarding/recipes is the RAM hog; Postgres at Linz scale is nothing.
- Ubuntu 24.04 LTS. Hardening first hour: ssh keys only, `ufw` (22/80/443),
  `fail2ban`, `unattended-upgrades`.
- Add a **Hetzner Storage Box** (EUR ~4/mo) for backups.

## 2. System setup (~1 h)

```sh
apt install postgresql-16 postgresql-16-postgis-3 postgresql-16-pgvector
curl -LsSf https://astral.sh/uv/install.sh | sh
git clone <remote> /opt/eventindex && cd /opt/eventindex && uv sync
uv run playwright install --with-deps chromium
# .env copied over (scp), same OPENROUTER/GOOGLE_PLACES keys, prod DATABASE_URL
uv run python -m eventindex.db.migrate
```

## 3. Data migration (~30 min, claims history must survive)

The event_claim/identity tables are append-only contracts - full dump, not
re-crawl:

```sh
# on the Mac                      # on the VPS
pg_dump -Fc eventindex > ei.dump  pg_restore -d eventindex ei.dump
```

Copy `var/` (trajectories, review dumps, digests) for continuity.

## 4. Services (systemd instead of Mac cron)

| Unit | What | Notes |
|---|---|---|
| `eventindex-api.service` | `uvicorn eventindex.api.app:app --host 127.0.0.1 --port 8000` | Restart=always |
| `eventindex-worker.service` | `python -m eventindex.jobs.worker` (loop mode, no `--once`) | Restart=always; replaces the 10-min cron |
| `eventindex-schedule.timer` | every 15 min | as today |
| `eventindex-digest.timer` | daily 23:55 | as today |
| `eventindex-discover.timer` | Mon 03:00 | as today |

Reverse proxy: **Caddy** (2-line config, automatic Let's Encrypt TLS):
`domain.tld { reverse_proxy 127.0.0.1:8000 }`. Caddy over nginx purely for
zero-maintenance TLS.

## 5. Backups + monitoring

- Nightly `pg_dump -Fc` -> Storage Box via cron, 14-day rotation. The DB IS
  the product; everything else is regenerable.
- The existing digest + dead-man's switch stay the observability stack
  (H7.3). Add one line to the digest cron: ping healthchecks.io so a dead
  *server* (not just dead crawls) alerts Alexander's phone.

## 6. Cutover checklist

1. Freeze: disable Mac crontab.
2. Final dump/restore + var/ sync.
3. Start services, run one `worker --once` cycle, check digest + `/llms.txt`.
4. DNS -> VPS, verify `https://domain/v1/occurrences` with the API key.
5. Re-subscribe Alexander's calendar .ics to the new domain.
6. Delete the Mac crontab (leave the dev DB container for local work).

## Cost & effort

~EUR 12-19/month total (VPS + storage box + domain). One focused day,
of which the first hour is the only fiddly part (hardening). Rollback story:
the Mac setup keeps working until step 6, so cutover risk is near zero.

## Pre-demo extras (before any authority sees it)

- `/privacy` static route: privacy policy (also a hard requirement for the
  ChatGPT app directory later).
- Scrape-posture one-pager (see GO-TO-MARKET.md).
- Rate limits on the API (deferred until now - "before a second consumer
  exists" arrives with the first public demo).

## Deployed 2026-07-08 (netcup, shared box)

Reality vs. the plan above: deployed to the existing netcup VPS (Debian 13,
12 vCPU/32GB, shared with other workloads) instead of a fresh Hetzner box.
DB runs as the repo's own Docker image on 127.0.0.1:5433 (port 5432 was
taken by another project; password in /root/.eventindex-dbpw). App native
under user `eventindex` at /opt/eventindex, cloned from GitHub. systemd:
eventindex-api (127.0.0.1:8400), eventindex-worker, timers for
schedule/digest/discover (Europe/Vienna OnCalendar). No public exposure
until domain+Caddy: access via `ssh -L 8400:localhost:8400 netcup`.
Deploy = `cd /opt/eventindex && git pull && uv sync && systemctl restart
eventindex-api eventindex-worker eventindex-worker2 eventindex-worker3`
(`uv sync` since 2026-07-20: pypdf).

**THREE worker units exist** (worker2/worker3 were added undocumented at
some point; discovered 2026-07-13 when a deploy restarted only
`eventindex-worker` and the other two kept processing jobs with pre-deploy
code, writing stale-schema enrichment rows). Restart ALL of them on every
deploy, and run `python -m eventindex.db.migrate` before the restart when
db/migrations gained a file. Whether 3 parallel workers should stay is an
open product/ops question (CLAUDE.md says one process).
