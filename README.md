# Wasup — Linz Event Index

**An AI-native index of every public event in Linz, Austria** — from the
Brucknerhaus concert down to the gym spinning class that exists only on the
gym's own website, the Pfarre flea market, and the run club that announces
on its own domain. Crawled, deduplicated, confidence-scored, and served
through an API that AI agents can use without any human in the loop.

Built almost entirely by AI coding agents, supervised by one human product
owner. The operating manual that makes that possible is in this repo.

## Why

Event portals rot the same way everywhere: mediocre coverage (only
organizers who bother submitting), stale data (nobody re-checks), and
filters stuck in 2010 (date + category). Meanwhile AI assistants need
exactly what portals never built: **complete, fresh, honestly-uncertain,
machine-queryable local data.**

This index inverts the model:

- **Coverage**: ~200 sources and growing via automatic discovery — the city
  portal's whole catalog plus 3× more events it never had. New sources go
  from URL to indexed events with zero human code (a budget-capped browser
  agent writes a declarative crawl "recipe"; a validation contract executes
  it before it's ever trusted).
- **Freshness**: nightly QA re-verification against source pages,
  cancellation sweeps, and confidence that *decays at query time* when a
  source stops confirming — a dead pipeline converges to an empty feed,
  never a confidently stale one.
- **Honesty as an API contract**: `null` means unknown and never matches a
  filter; every inferred attribute carries a certainty (capped, evidence-
  gated); forward-projected occurrences are flagged `projected`; every
  event exposes sanitized per-source provenance without publishing the raw
  append-only claim payloads.
- **Audience attributes no portal has**: expected age range, gender split,
  energy, kid/newcomer/solo-friendliness, and whether the event's *format*
  forces interaction (`interaction_structure`) — each an explicit estimate
  with confidence, queryable as weighted preferences.

## For AI agents (the point of the whole thing)

```
GET  /llms.txt                  the instruction sheet (llmstxt.org convention)
GET  /.well-known/api-catalog   RFC 9727 discovery
POST /v1/query                  structured search - the CALLING agent parses
                                natural language; the index runs no LLM per query
POST /mcp                       MCP server (streamable HTTP, stateless, no auth):
                                the same read surface as tools, for ChatGPT
                                apps/connectors and Claude connectors
```

Query semantics: hard guarantees (time window, exclusions, categories,
price) as set logic; audience attributes as **soft preferences** ranked by
`importance × stored certainty` — confident matches > weak guesses >
unknowns > contradictions, nothing silently dropped, `match_score` exposed.

```json
POST /v1/query
{"from_dt": "2026-07-11T08:00", "include_terms": ["lauf", "run"],
 "newcomer_friendly": true, "importance": {"newcomer_friendly": 1.0}}
```

Also: `GET /v1/occurrences` (raw listing), `GET /v1/events/{id}` (sanitized
event detail + source provenance), `GET /v1/feed.ics` (any filter combo as a calendar
subscription), `GET /v1/changes` (delta stream), `POST /v1/reports`
(feedback feeds source trust). A drop-in Claude skill lives in
`skills/wasup/`.

## Architecture (one paragraph)

One Python monolith, one Postgres (PostGIS + pgvector). Every pipeline
stage is a pure function; the only queue is a jobs table with
`SELECT ... FOR UPDATE SKIP LOCKED`. Crawled facts land as **append-only
claims**; the canonical index is a deterministic, rebuildable function of
the claims log (dedup via fingerprint blocking + weighted matching + LLM
grey-zone adjudication, gated by a labeled gold set with a hard
precision-at-merge floor). Recurrence is a constrained schema compiled
deterministically to RRULEs — the LLM never writes dates. Every LLM call
runs inside an enforced budget context; a global daily cap is checked in
code before any token is spent. Cheap open-weight models by default; an
escalation ladder and validation nets guarantee quality instead of model
brand.

## Running it

```sh
./scripts/dev_db.sh                       # Postgres 16 + PostGIS + pgvector (Docker)
cp .env.example .env                      # add your OPENROUTER_API_KEY
uv sync
uv run python -m eventindex.db.migrate
uv run python -m eventindex.jobs.worker   # the worker loop (--once to drain and exit)
uv run python -m eventindex.jobs.schedule # enqueue due crawls + daily QA (cron: */15)
uv run python -m eventindex.jobs.digest   # nightly observability digest
uv run uvicorn eventindex.api.app:app     # the API; agent docs at GET /llms.txt
uv run python scripts/create_api_key.py me  # keys: the API is open until the first key exists
uv run pytest                             # test suite (uses a separate eventindex_test db)
```

Deployment target: one small VPS, systemd, no orchestration
(`docs/VPS-DEPLOYMENT.md`).

## The AI-first development kit

This repo doubles as a working example of spec-driven autonomous
development. The agent's constitution, in authority order:

| File | Role |
|---|---|
| `CLAUDE.md` | Operating rules: decision protocol, anti-overengineering constraints, scope fences, verification discipline |
| `DECISIONS.md` | Locked decisions + the agent-maintained changelog |
| `BUILD-PLAN.md` | Phases with done-criteria demonstrated on real data |
| `specs/ARCHITECTURE.md` | Full system design |
| `specs/HURDLES.md` | Every hard problem decomposed until boring |
| `specs/USE-CASE-VALIDATION.md` | 22 adversarial user scenarios the design must survive |
| `OPEN-QUESTIONS.md` | The human/agent interface |

Prime directive: one monolith, one Postgres, pure-function stages, claims
append-only, canon rebuildable, null means unknown, nothing built before
its trigger fires.

## Crawling posture

Honest User-Agent with a contact address, per-domain rate limits (≥2s),
conditional GETs and content-hash early exits, private-intent suppression
heuristics (residential-looking events are published without location),
and a takedown contact. Public event data only.

## Status & license

Live for Linz (~1,500 events, ~3,000 upcoming occurrences, ~200 sources);
built for multi-city from day one. **All rights reserved** — the code is
public to read; talk to me about using it: alexander.krauck@gmail.com
