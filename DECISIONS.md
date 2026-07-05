# Locked Decisions

These are settled. The coding agent follows them without revisiting (CLAUDE.md Class A). Changing anything here requires Alexander's explicit sign-off via OPEN-QUESTIONS.md.

## Stack

| Area | Decision | Rationale (short) |
|---|---|---|
| Language | Python 3.12+ | ecosystem fit (Playwright, LLM SDKs, dateutil), Alexander's stack |
| Package/env | `uv` | fast, lockfile, no poetry ceremony |
| Web framework | FastAPI | standard, pydantic-native |
| DB | PostgreSQL 16 + PostGIS + pgvector, single instance | ARCHITECTURE §11; one box is 100x headroom |
| DB access | psycopg3, **plain SQL** (no ORM) | schema is the spec; ORMs hide the queries that matter |
| Migrations | plain sequential SQL files + tiny runner (or `dbmate`) | no alembic autogen magic |
| Validation | pydantic v2 everywhere (LLM outputs, API IO, recipes) | one validation idiom |
| Job queue | `jobs` table + `SELECT … FOR UPDATE SKIP LOCKED`, one worker process | ARCHITECTURE §H7; no broker |
| Scheduler | cron (system) inserting jobs | boring |
| HTTP fetch | httpx (async, in fetcher only) | |
| Headless | Playwright, lazy-installed, phase 3 only | ~20-30% of sources need it |
| Recurrence | constrained schema → deterministic compiler → `dateutil.rrule` | HURDLES §H1: LLM never writes RRULE |
| LLM access | one thin client module wrapping a single provider SDK; model names in config | mini-model default, escalation per ARCHITECTURE §model-routing |
| Embeddings | provider API (same client), stored in pgvector | no local models in v1 |
| Tests | pytest; fixtures = recorded HTML snapshots (HURDLES §H3.4); gold set as data file in repo | |
| Deploy | one small VPS (Hetzner-class), systemd services (api, worker), Postgres on same box | no docker required; if containerizing, ONE Dockerfile max |
| Repo layout | single repo, single package `eventindex/`, modules: `db/ fetch/ extract/ resolve/ enrich/ api/ discovery/ jobs/` | monolith |
| Secrets | `.env` file, never committed | |
| Timezone | store UTC, business logic in Europe/Vienna | Austrian holidays table per HURDLES §H1.2 |

## Product decisions (locked)

- Claims are **append-only**; canon is a rebuildable materialized view (HURDLES §H0). The `identity` table is the only non-rebuildable canon state.
- Staleness decay computed **at query time** in the API layer (ARCHITECTURE §7) - never a batch job.
- `null` means **unknown**, never "no" - hard API contract (ARCHITECTURE §7/§9b).
- Negative constraints (exclusions) are set logic before ranking, never embedding similarity (ARCHITECTURE §7).
- Status/availability merge is asymmetric recency-first (ARCHITECTURE §6).
- False-merge is the bad dedup error: precision@merge ≥ 0.98 against gold set, recall may lag (HURDLES §H2.3).
- Crawl politeness: respect robots.txt on tier 2-3, honest UA with contact email, per-domain rate limit ≥ 2s.
- Budgets: every LLM/agent call runs inside a budget context; global daily cap enforced in code from day one. Initial caps: €5/day global LLM, per-source defaults per ARCHITECTURE §cost-governance.
- v1 scope fence per CLAUDE.md - re-entry triggers per HURDLES §H7.2.
- Metro boundary: configurable polygon, initially Linz + ~25km (Leonding, Traun, Ansfelden, Enns, Wels-fringe optional) - index generously, filter at serve time (HURDLES §H4.3).

## Deliberately NOT decided (Class B - agent's choice, log below)

Internal module APIs, test organization, exact index DDL, digest formatting, log format, retry/backoff parameters (within budget caps), fingerprint normalization details (within §6 spec).

## Changelog (agent appends one-liners here)

- 2026-07-03: kit created; no code exists yet.
- 2026-07-03: OPEN-QUESTIONS #1-3, #6 answered (OpenRouter, local Docker Postgres for dev, gmail contact, local git); details there.
- 2026-07-03: phase 0 built. Class B picks: src/ layout (uv default), initial models mini=openai/gpt-5-mini mid=anthropic/claude-sonnet-4.5 frontier=anthropic/claude-opus-4.5, retry backoff 60s×5^n (3 attempts), digest to var/digests/ as text file. Dev DB image is pgvector base + PostGIS apt package (postgis/postgis has no arm64).
- 2026-07-03: phase 1 built. Class B picks: fetcher is sync httpx for now (one source/job + 2s domain rate limit = zero concurrency to exploit; async when the scheduler batches); budget ledger writes on own autocommit connection (spend must survive job rollback - money is already gone); linztermine open-data XML gets a dedicated tier-b parser (§3.3 "platform API payload"; a 2nd API format triggers generalizing); 15 top-level category seeds in config; meinbezirk dropped from seeds (nation-mixed feed, region pollution); LLM claims: one claim per date element, 60d horizon.
- 2026-07-05: phase 2 built. Class B picks: title_similarity is trigram-only until an embeddings provider is decided (→ OPEN-QUESTIONS #9); §6 blocking loosened to (Vienna day × venue-cell) with the §6 formula deciding within blocks (title-exact blocking would make cross-source title variants unmergeable); resolve job = full rebuild, delete+insert in one tx instead of table-rename blue-green (atomic via MVCC at 2k rows); LLM adjudications + recurrence verifications cached in `adjudication` table on own connections (paid verdicts survive rollbacks, rebuilds are free and idempotent); occurrence ids = uuid5(event_id, starts_at); identity repoints to survivor on merge, mints fresh id on lineage split; explicit-date series threshold >= 3 distinct days; theater runs of 1-2 days stay per-day one_offs.
