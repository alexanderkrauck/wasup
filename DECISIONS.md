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
| HTTP fetch | httpx (sync for now - one source/job + 2s domain rate limit leaves no concurrency to exploit; async when that changes) | changelog 2026-07-03 |
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
- Crawl politeness: honest UA with contact email, per-domain rate limit ≥ 2s. robots.txt is deliberately ignored (Alexander's explicit sign-off, 2026-07-06: "as long as it's not behind a login wall we must always crawl"). Login walls stay out of scope.
- Probe policy: every candidate scoring ≥ 0.5 registers automatically; doubts become `extraction_hint.probe_concerns` attributes (e.g. membership_required) instead of gating registration (Alexander, 2026-07-06: when in doubt, crawl). Junk decays economically (H4.2).
- Budgets: every LLM/agent call runs inside a budget context; global daily cap enforced in code from day one. Caps: €10/day global LLM (raised from €5 by Alexander 2026-07-06 during discovery ramp-up), per-source defaults per ARCHITECTURE §cost-governance.
- v1 scope fence per CLAUDE.md - re-entry triggers per HURDLES §H7.2.
- Metro boundary: configurable polygon, initially Linz + ~25km (Leonding, Traun, Ansfelden, Enns, Wels-fringe optional) - index generously, filter at serve time (HURDLES §H4.3).

## Deliberately NOT decided (Class B - agent's choice, log below)

Internal module APIs, test organization, exact index DDL, digest formatting, log format, retry/backoff parameters (within budget caps), fingerprint normalization details (within §6 spec).

## Changelog (agent appends one-liners here)

- 2026-07-03: kit created; no code exists yet.
- 2026-07-03: OPEN-QUESTIONS #1-3, #6 answered (OpenRouter, local Docker Postgres for dev, gmail contact, local git); details there.
- 2026-07-03: phase 0 built. Class B picks: src/ layout (uv default), initial models mini=openai/gpt-5-mini mid=anthropic/claude-sonnet-4.5 frontier=anthropic/claude-opus-4.5, retry backoff 60s×5^n (3 attempts), digest to var/digests/ as text file. Dev DB image is pgvector base + PostGIS apt package (postgis/postgis has no arm64).
- 2026-07-03: phase 1 built. Class B picks: fetcher is sync httpx for now (one source/job + 2s domain rate limit = zero concurrency to exploit; async when the scheduler batches); budget ledger writes on own autocommit connection (spend must survive job rollback - money is already gone); linztermine open-data XML gets a dedicated tier-b parser (§3.3 "platform API payload"; a 2nd API format triggers generalizing); 15 top-level category seeds in config; meinbezirk dropped from seeds (nation-mixed feed, region pollution); LLM claims: one claim per date element, 60d horizon.
- 2026-07-07: models swapped to open-weight Chinese models (Alexander: cost): mini=deepseek/deepseek-v4-flash, mid=moonshotai/kimi-k2.6 (onboarding/agentic), frontier=z-ai/glm-5.2. Est. ~4x cheaper/day vs gpt-5-mini+sonnet at same volume. Verification pending credit top-up: first cron cycle exercises all schema-validated paths; gold set + recipe self-validation gate quality. Caches (adjudication, enrichment) keep prior verdicts.
- 2026-07-06 (later): H5 "never invent" relaxed by Alexander: inferred attributes are ALWAYS estimated (LLM world knowledge), confidence encodes guess-ness (~0.2 guess / ~0.35 typical / <=0.8 evidenced). Null only for truly inapplicable. Rationale: the confidence field exists to label guesses; null starves agent-search filters.
- 2026-07-06: demographics-inference scope fence re-entry trigger fired by Alexander ("we want the age estimate... rich llm inferred attributes, each with confidence"). Enrichment per H5: category_priors baseline, LLM adjusts ONLY from explicit textual evidence, confidence capped 0.8, served as labeled estimates. Cached by content hash so canon rebuilds stay free.
- 2026-07-06: phase-4 /v1/search redefined by Alexander: agent-parsed HARD filters first (time window, category/tags, audience, price), set logic in SQL; embeddings at most rank the residual vibe fragment, never select. Note: audience-age filters need the H5 demographics estimator (scope-fence re-entry trigger "product demand" arguably fired; decide at phase-4 start).
- 2026-07-06: Alexander overrode two politeness/gating rules in chat: robots.txt ignored everywhere (login walls remain the only boundary); probe review queue abolished - >=0.5 always registers with probe_concerns attributes. Both edited above in place.
- 2026-07-05: phase 2 built. Class B picks: title_similarity is trigram-only until an embeddings provider is decided (→ OPEN-QUESTIONS #9); §6 blocking loosened to (Vienna day × venue-cell) with the §6 formula deciding within blocks (title-exact blocking would make cross-source title variants unmergeable); resolve job = full rebuild, delete+insert in one tx instead of table-rename blue-green (atomic via MVCC at 2k rows); LLM adjudications + recurrence verifications cached in `adjudication` table on own connections (paid verdicts survive rollbacks, rebuilds are free and idempotent); occurrence ids = uuid5(event_id, starts_at); identity repoints to survivor on merge, mints fresh id on lineage split; explicit-date series threshold >= 3 distinct days; theater runs of 1-2 days stay per-day one_offs.
- 2026-07-07 (one-shot): forward projection shipped (red-team #1): deterministic gap detection (weekly/biweekly, >=3 dates, one holiday-skip tolerated) on explicit series; projects max 4 weeks past last observation and ONLY beyond the sources' demonstrated horizon (extraction_hint.horizon_days) - absence within a feed's reach is evidence, never filled. Projected occurrences carry `occurrence.projected` (migration 010) end-to-end; canon-rebuild semantics make projections self-expiring. Free-text recurrence ("jeden Mittwoch...") is regex-gated into the constrained Recurrence schema, cached by content hash in `text_recurrence`.
- 2026-07-07 (one-shot): adjudicator upgraded (red-team #2): prompt now carries source names, 200-char description snippets, price, URL; mini "different" verdicts at score >= 0.65 get ONE mid-model second opinion (decided_by=llm_mid); adjudicated cross-venue merges auto-alias venues < 300m apart (else review dump). 342 stale mini "different" verdicts invalidated for re-decision. Gold set grown 124->155 rows (31 new labels incl. the cached-miss marquee pairs); precision@merge 1.000 over 23 system merges. Fixed a latent cache poisoner: failed LLM adjudications/verifies (incl. BudgetExceeded) are no longer cached as verdicts; budget errors now bubble so the worker parks the rebuild until the daily reset.
- 2026-07-07 (one-shot): phase-4 remainder shipped: qa_check job (nightly 20-sample + per-report; cancellations flow as claims from an internal 'QA verifier' source per H0, never canon edits; source trust EMA 0.9/0.1), digest QA section, GET /v1/feed.ics (shared filter set with /v1/occurrences), POST /v1/reports -> qa_check, GET /v1/changes (updated_at keyset), api_key table + one dependency function (bootstrap rule: API open until the first active key exists; key accepted as X-API-Key header or ?api_key= for calendar clients), §9b private-intent suppression (residential address + personal-name organizer + no venue -> geo withheld, event stays listed, review dump), 50-query search-leak gate as pytest -m live.
- 2026-07-08 (backfill for 2026-07-07 chat decisions previously only in config.py comments): mid model is moonshotai/kimi-k2.7-code, not k2.6 (k2.7-code is multimodal, covers onboarding + adjudication escalation); frontier tier DELETED (Alexander: "2 tiers might be sufficient" - it had zero call sites; re-add if tier-D crawls ever unfence). Current models: mini=deepseek/deepseek-v4-flash, mid=moonshotai/kimi-k2.7-code.
- 2026-07-08: doc-rot sweep (Alexander requested review): stale spec text annotated in place with pointers here (ARCHITECTURE §2/§6/§9/§11/§12, HURDLES H5/H7.2, BUILD-PLAN phase 1); .env.example dead Google-CSE vars removed; README run instructions synced with the installed cron + API. Historical records (migrations, old changelog lines) left untouched by policy.
