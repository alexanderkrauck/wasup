# Coding Agent Operating Manual - Linz Event Index

You are building this system **AI-first**. The human (Alexander) acts as product owner only: he gives feedback and answers open questions. He does not write code, does not review every line, and will not catch your mistakes for you. Therefore the rules below are hard constraints, not suggestions - they exist so your autonomous decisions cannot create tech debt or overengineering.

## Document authority (on conflict, higher wins)

1. `DECISIONS.md` - locked decisions. Never revisit, never "improve".
2. This file - operating rules.
3. `BUILD-PLAN.md` - what to build, in what order, with done-criteria.
4. `specs/ARCHITECTURE.md` - full system design (§ references in the plan point here).
5. `specs/HURDLES.md` - how each hard part was de-risked; follow these solutions.
6. `specs/USE-CASE-VALIDATION.md` - why features exist; consult when a requirement seems odd.

If specs conflict or something is underspecified, do NOT improvise on anything non-trivial: add it to `OPEN-QUESTIONS.md` and continue with other work.

## Decision protocol (the core rule)

Classify every decision you face:

**Class A - locked.** Already in DECISIONS.md. Follow it. Do not re-litigate, do not add abstraction layers "in case we switch later".

**Class B - trivial and reversible.** Naming, file layout within the given structure, test structure, internal function signatures, choice among ways to write the same query. Decide yourself. If notable, append one line to the changelog in DECISIONS.md.

**Class C - stop and ask.** Any of these triggers means write it to OPEN-QUESTIONS.md and do NOT proceed on that item:
- adds a new infrastructure component (any datastore, queue, cache, container, service)
- adds a new external dependency with a nontrivial footprint (rule of thumb: new lib >1k stars needed for core path = fine if it's the standard tool; anything exotic, unmaintained, or overlapping with an existing dep = ask)
- adds recurring cost or an external account/API key
- changes the DB schema of `event_claim` or `identity` (append-only contracts)
- deviates from specs/ARCHITECTURE.md in behavior, not just implementation detail
- security- or privacy-relevant (anything touching §9b)
- anything you find yourself justifying with "in the future we might need..."

That last trigger is the important one. **"We might need it later" is always Class C.** Later is when we build it.

## Anti-overengineering rules (hard)

- **One process, one Postgres.** No Kafka, no Redis, no RabbitMQ, no k8s, no docker-compose orchestras, no microservices, no serverless. The jobs table with `SKIP LOCKED` (ARCHITECTURE §H7/12) is the only queue. Target scale is ~1200 sources, ~200 jobs/hour, ~50k occurrences/year - a Raspberry Pi could serve this. Design for 10x that, not 1000x.
- **No abstraction before the third concrete use.** No plugin systems, no strategy-pattern registries, no generic "provider interfaces" with one implementation. The extraction cascade and pagination taxonomy in the specs are the only sanctioned extension points.
- **No config for things with one value.** Constants in one `config.py` are fine. No YAML hierarchies, no feature-flag framework, no env-var sprawl beyond secrets + a handful of knobs.
- **Sync by default.** Async only where the work is actually IO-bound and concurrent (the fetcher). Never async "for consistency".
- **No caching layer until a measured query is slow.** Postgres with proper indexes is the cache.
- **No admin UI, no dashboard framework.** Observability = `crawl_log` table + nightly digest (ARCHITECTURE §H7.3). Review queues are markdown/JSON dumps Alexander handles in chat.
- **No auth framework.** API keys in a table, checked in one middleware function.
- **Every pipeline stage is a pure function** `(payload, tx) -> rows to insert / jobs to enqueue`. No in-memory state, no singletons holding data, no background threads beyond the worker loop.
- **Delete code instead of flagging it off.** Git remembers.

## Scope fence (v1)

Explicitly FORBIDDEN in v1, even as stubs, interfaces, or "preparation" (HURDLES §H7.2 - each has a re-entry trigger, none has fired):
- socials scraping (Instagram/Facebook/Telegram)
- ~~vision/PDF extraction path~~ — re-entry trigger FIRED 2026-07-20 (Alexander: human-parity extraction is THE requirement; anything a human can extract from a non-login, non-video source, the system extracts). In scope: PDF text tier, poster/screenshot vision tier, both through the one validated payload path.
- ~~tier-D agentic crawls~~ — re-entry trigger FIRED 2026-07-20 (same requirement): the extractor agent is the ladder's last rung — escalation-only (self-heal, low-yield, degraded cadence, parity audit, agentic-mode sources), budget-ringed, never the default crawl path.
- ~~demographics/gender/fullness *inference*~~ — re-entry trigger FIRED 2026-07-06 (Alexander: agent search needs rich inferred attributes). In scope per H5: priors + explicit-text-evidence only, confidence-capped, labeled estimates.
- takedown self-service endpoint (manual email suffices; the suppression heuristics of §9b ARE in scope)
- ~~any frontend~~ — re-entry trigger FIRED 2026-07-09 (Alexander: visualization page). In scope: exactly one dependency-free HTML calendar page over the public read API (`GET /`). Still forbidden: frameworks, SPA, build step, any second page without a new trigger.
- multi-city support (no `city_id` columns "for later" - the design is city-agnostic by nature of the source registry; that is enough)

Building any of these early = the exact tech-debt failure mode this file exists to prevent.

## Verification discipline

- A phase is done when its done-criterion (BUILD-PLAN.md) is demonstrated **against real Linz data**, not fixtures. Post the evidence (query + output) in the phase completion note.
- Every extractor/resolver change runs against the gold set (HURDLES §H2) and fixture replays (§H3.4) before merge. Falling precision = blocked merge, no exceptions.
- All LLM calls: structured output, validated against pydantic schemas, with the deterministic sanity checks from ARCHITECTURE §7 (dates parse, geo in bounds, schema valid). An unvalidated LLM output reaching the DB is a bug by definition.
- Budget guardrails (ARCHITECTURE §5b/cost governance) are implemented in phase 0, not retrofitted. No LLM call happens outside a budget context.
- Write tests for behavior, not coverage. The valuable tests: recurrence compiler, fingerprinting, merge logic, staleness decay math, API filter semantics (null = unknown!).

## Working style

- Small, self-contained increments; each leaves the system runnable.
- Update `DECISIONS.md` changelog and `OPEN-QUESTIONS.md` as you go; they are the interface to the human. Questions should be batched, concrete, and answerable in one sentence each ("Hetzner CX22 ok? y/n", not "what are your thoughts on hosting").
- When blocked on an open question, switch to unblocked work. Never invent an answer to keep moving on the blocked item.
- Commit messages state *what* and *why-if-not-obvious*. No changelogs in code comments.
- German-language content (source text, test fixtures) is normal - don't translate data, only code/comments/docs are English.
