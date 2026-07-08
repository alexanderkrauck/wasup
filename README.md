# Linz Event Index - Agent Starter Kit

An index (not a platform) of **every** event in Linz - from Brucknerhaus down to the gym spinning class only posted on the gym's own website. Crawled, extracted, deduplicated, confidence-scored, served via API.

**This folder is a complete handoff to a coding agent.** Development is AI-first: the human gives feedback and answers `OPEN-QUESTIONS.md`; the agent builds everything else.

## Read in this order

| File | What it is |
|---|---|
| `CLAUDE.md` | Operating rules for the coding agent: decision protocol, anti-overengineering constraints, v1 scope fence, verification discipline. **Binding.** |
| `DECISIONS.md` | Locked stack + product decisions. Never re-litigated. Has a changelog the agent maintains. |
| `BUILD-PLAN.md` | Phases 0-4 with done-criteria (demonstrated on real Linz data) and the human's few touchpoints per phase. |
| `OPEN-QUESTIONS.md` | The human/agent interface. Agent appends concrete questions; Alexander answers inline. |
| `specs/ARCHITECTURE.md` | Full system design: data model, core routine, recipe crawler, discovery, dedup, confidence, API, source catalog, business model. |
| `specs/HURDLES.md` | Every hard problem recursively decomposed until boring. Follow these solutions - they are why the build is assembly, not invention. |
| `specs/USE-CASE-VALIDATION.md` | 22 adversarial user scenarios graded against the design - the "why" behind odd-looking requirements. |

## Running (dev)

```sh
./scripts/dev_db.sh                       # build + start Postgres (PostGIS+pgvector) container
cp .env.example .env                      # then fill in OPENROUTER_API_KEY
uv sync
uv run python -m eventindex.db.migrate    # apply db/migrations/*.sql
uv run python -m eventindex.jobs.worker   # the worker loop (--once to drain and exit)
uv run python -m eventindex.jobs.schedule # enqueue due crawls + daily QA sample
uv run python -m eventindex.jobs.digest   # write today's digest to var/digests/
uv run uvicorn eventindex.api.app:app     # the API; agent docs: GET /llms.txt (POST /v1/query = zero-LLM search)
uv run python scripts/create_api_key.py <name>  # mint an API key; the API is open until the first key exists

# installed crontab (self-sustaining index; `crontab -l` is the source of truth):
#   */15 * * * *  schedule        (due crawls, completeness escalation, nightly QA enqueue)
#   */10 * * * *  worker --once   (drain ready jobs)
#   55 23 * * *   digest
#   0 3 * * 1     schedule --discover   (weekly discovery sweeps)
uv run pytest                             # tests (use the eventindex_test db)
uv run pytest -m live tests/test_search_live.py  # 50-query search gate: live DB, spends cents
```

## Prime directive (short version)

One monolith, one Postgres, pure-function pipeline stages, mini-models by default, claims append-only, canon rebuildable, null means unknown, nothing built before its trigger fires. When in doubt: it's a Class C decision - ask, don't guess.
