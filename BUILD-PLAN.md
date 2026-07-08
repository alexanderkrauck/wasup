# Build Plan - dependency-ordered phases

Each phase is independently shippable and ends with its **done-criterion demonstrated on real Linz data**. No calendar - the constraint is verification, not typing. Do not start a phase before the previous one's criterion is demonstrated (exception: prep work explicitly listed as parallel-safe).

Alexander's involvement per phase is listed as **[human]** items - keep them few and batched.

## Phase 0 - Foundation

Repo scaffold, `config.py`, DB schema + migrations (ALL tables from ARCHITECTURE §2 incl. the columns v1 leaves null), `jobs` table + worker loop (pure-function stages, SKIP LOCKED), budget context (global daily cap + per-source ledger), LLM client wrapper with pydantic-validated structured output, `crawl_log`, nightly digest (email or file), dead-man's switch (no successful crawl in 48h → digest screams).

**Done when:** a dummy `crawl` job round-trips through the worker loop, writes `crawl_log`, spends from a budget, and shows up in a digest.
**Not in this phase:** any real fetching, any API endpoints.
**[human]:** provide LLM API key, VPS or "run locally for now", contact email for the crawler UA. (→ OPEN-QUESTIONS #1-3)

## Phase 1 - Skeleton: real events queryable

Fetcher (httpx, ~~robots.txt~~ *ignored per locked decision 2026-07-06*, rate limits, content-hash early exit, conditional GET), extraction cascade tiers a-c: JSON-LD schema.org/Event parser, ICS/RSS parser, LLM full-text extractor (mini model, per ARCHITECTURE §3.3) - each emitting `event_claim` rows. Seed registry: ~40 tier-1/2 sources hand-entered from ARCHITECTURE §10 (start with linztermine, eventfinder, linz.at, oeticket, Eventbrite browse pages [JSON-LD, see §catalog], Posthof, Brucknerhaus, Stadtwerkstatt, Kapu, VHS, AEC, JKU...). Minimal resolver v0: fingerprint-identical claims collapse; naive canon projection. `GET /v1/occurrences` with time/geo/category filters + keyset pagination; `GET /v1/events/{id}`.

**Done when:** `curl /v1/occurrences?from=<today>&near=48.3069,14.2858&radius=5km` returns real, current Linz events from ≥10 distinct sources, with provenance visible.
**Not in this phase:** dedup across sources, recurrence, discovery, semantic search.
**[human]:** eyeball the first 50 indexed events for obvious garbage (15 min).

## Phase 2 - Resolve & recur: one event, once

Venue table + canonicalization (GMaps place_id where available, trigram alias matching, review-queue dump for new venues - HURDLES §H2.1). Fingerprint blocking + weighted match + LLM grey-zone adjudication (ARCHITECTURE §6). Asymmetric status/availability merge (§6). Full canon-as-materialized-view rebuild (`resolve(all_claims) → canon_next` swap, identity table - HURDLES §H0). Recurrence: constrained schema → compiler → RRULE → occurrence expansion, OÖ holiday/Ferien table, verify-call, series fingerprints (HURDLES §H1). Query-time staleness decay in API. **Gold set**: dump ~150 candidate claim pairs for labeling; wire as regression test.

**Done when:** (a) one real concert present on ≥3 sources appears exactly once with merged fields + provenance; (b) a real weekly class from a gym/VHS timetable produces correct occurrences 8 weeks out, skipping a real holiday; (c) full canon rebuild from claims is idempotent (two rebuilds → identical output); (d) gold-set precision@merge ≥ 0.98.
**[human]:** the gold-set labeling session (~2h, one sitting, tooling: a simple CSV the agent prepares) + venue review queue (~5 min/week from here on).

## Phase 3 - Discovery + recipes: zero-code source onboarding

Recipe schema + interpreter (pagination taxonomy, stop conditions, validation contracts, fixture-replay tests - ARCHITECTURE §5b, HURDLES §H3). Onboarding agent on the chosen harness (Playwright tools, `emit_events`, trajectory logging, budget enforcement - ARCHITECTURE §harness). Self-validation at recipe birth; self-healing on validation failure/yield drop. Discovery: GMaps/OSM places sweep → probe classifier → auto-register/review-queue (§4a, H4); portal backlink mining (§4b); registry sweeps for ZVR/ASKÖ/Union/Diözese/VHS directories (§4e); search fan-out (§4d). Adaptive scheduler (yield_ema, proximity boost, priority = value/cost). 72h/day-of cancellation classifier on changed pages (§H6.2).

**Done when:** (a) a never-before-seen URL goes from `probe → onboard → recipe → indexed events` with zero human code; (b) registry ≥ 400 active sources; (c) a manufactured cancellation test (fixture) flips an occurrence to cancelled and sweeps sibling sources; (d) weekly spend within budget caps.
**Not in this phase:** socials, vision/PDF, tier-D (scope fence).
**[human]:** GMaps API key decision (→ OPEN-QUESTIONS #4), weekly 5-min review-queue batches, spot-check 20 discovered sources.

## Phase 4 - Intelligence: the product feel

Confidence wiring end-to-end (source trust EMA, compound event confidence, staleness decay verified). Nightly QA sampler (re-verify N random events, feed trust). Embeddings + hybrid semantic search (`/v1/search`, `q=` on occurrences; exclusions as set logic BEFORE ranking). `.ics` feed endpoint with filter params. `/v1/reports` (user feedback → QA queue → trust). `/v1/changes` cursor stream. Booking-schema extraction where trivially present (ticket URL, "Anmeldung unter..." - full §13 agent-action layer stays future). API keys + rate limits. §9b suppression heuristics.

**Done when:** (a) "was geht heute abend, nicht techno, unter 20€" via `/v1/search` returns sensible ranked results with zero excluded-category leaks over a 50-query test set; (b) Alexander subscribes to a filtered .ics and events appear in his calendar; (c) a `/reports` submission demonstrably lowers a source's trust; (d) digest shows the QA loop running nightly.
**[human]:** use it for a week; the feedback from real usage IS the phase-4 acceptance test.

*Status 2026-07-07 (corrected 07-08): phase-4 components shipped (qa_check, .ics, /reports, /changes, api keys, §9b suppression, 50-query gate at tests/test_search_live.py) EXCEPT booking-schema extraction (ticket URL, "Anmeldung unter...") - unshipped, now red-team backlog #8. Evidence pending budget reset: (a) run `uv run pytest -m live tests/test_search_live.py`, (c)+(d) flow from the queue. (b) needs Alexander: subscribe to /v1/feed.ics?api_key=... Embeddings for ranking remain deliberately unbuilt (agent-search decision 2026-07-06: vibe-term overlap ranks; add pgvector ranking only if real usage shows it lacking). Rate limits deferred until a second consumer exists.*

## After v1 (do not touch until trigger fires - HURDLES §H7.2)

socials (trigger: ≥20 Instagram-only sources identified; ~8 suspected as of 2026-07-07) · vision/PDF (≥10 high-value PDF sources queued; ~2) · tier-D (≥5 sources defeat recipes; 1) · ~~demographics inference~~ trigger FIRED 2026-07-06, shipped as enrichment (DECISIONS changelog) · takedown self-service (first real request) · frontend (v1 proves usage via API/.ics first) · multi-city (Linz coverage bar demonstrably met).

## Red-team backlog (2026-07-07, two audit rounds - ordered by leverage)

1. ~~**Implicit-series forward projection**~~ SHIPPED 2026-07-07 (resolve/projection.py, occurrence.projected, text_recurrence cache; horizon-gated, self-expiring). Series need >=3 observed dates, so the day-curve lift compounds as claim history accumulates - watch `SELECT count(*) FROM occurrence WHERE projected`.
2. ~~**Marquee duplication regressed**~~ SHIPPED 2026-07-07 (evidence-rich adjudicator + mid escalation at score>=0.65 + venue aliasing; 342 stale mini "different" verdicts invalidated). Verify after the next funded rebuild: the Mariendom/Taschenlampen/Senso pairs must merge; gold precision gate stays blocking.
3. **Registered != yielding**: digest should flag sources with 0 claims after N crawls (HWYD Wix, strom.stwst.at). Also: sub-page probes for known domains with low yield (posthof.at/festivals case).
4. **Extraction quality**: times lost to 00:00 when source states them (Chet Faker 19:30); "F.I.T.C.: Pflasterspektakel Tag 1" mislabeling; Einlass/Beginn rule shipped but old claims persist until re-crawl.
5. **Vision/PDF fence trigger evidence**: Aktiv-Tage Linz (city Ferienprogramm) is a PDF brochure; count such high-value PDF sources toward the >=10 trigger (currently ~2).
6. **Socials fence**: ~8 suspected IG-first sources found in one 2-week window (trigger: >=20 confirmed). Start tracking suspected_ig_only on sources.
7. Special-handling sources: oeticket (DataDome bot protection - maybe skip, linztermine overlaps), meetup.com (JS/API), casinos.at (JS). Structural no-source-yet: beach volleyball, Salonschiff, Plus City, Biergarten live music.
8. **Booking-schema extraction** (phase-4 leftover, found in the 2026-07-08 doc sweep): ticket URL + "Anmeldung unter..." where trivially present in already-extracted text -> `booking_url`/`registration_required` columns (exist, always null). Full §13 agent-action layer stays future.

## Standing rules

- Every phase ends with: update DECISIONS changelog, batch open questions, post done-criterion evidence.
- Real-data checks always against live Linz sources; fixture tests for regression only.
- If a phase reveals a spec gap → OPEN-QUESTIONS.md, not silent improvisation.
