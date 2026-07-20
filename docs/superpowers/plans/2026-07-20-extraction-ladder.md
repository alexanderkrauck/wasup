# Extraction Ladder Implementation Plan (human-parity requirement)

**Requirement (Alexander, 2026-07-20, locked):** any non-login, non-video source a
human can extract events from, the system extracts. Fence triggers FIRED:
vision/PDF path + tier-D agentic extraction (recorded in CLAUDE.md/DECISIONS.md).

**Architecture: a 3-rung ladder, cheap by default, complete under failure.**
1. Recipes + deterministic cascade tiers (jsonld, ics/rss, **json_api NEW**,
   **pdf NEW**, selectors) — fast, free-ish, every crawl.
2. LLM text cascade (exists).
3. Extractor agent (onboarding agent + **emit_events NEW** + **vision NEW**):
   output is CLAIMS (index never dark) + recipe recompilation. Job kind
   `agent_extract`. Budget = existing onboard rings + per-source monthly caps.

**Root-cause evidence (prod trajectory, factory300):** the agent found the
Nexudus JSON API and emitted the correct recipe 3x; `run_recipe`'s cascade
cannot parse JSON bodies (jsonld→llm_text on raw JSON, 20k-char truncation cuts
`CalendarEvents` out) → "items 0 < min_items 3" → exhausted. Fix the
interpreter, not the agent.

## Phases (commit per phase, suite green each time)

1. **json_api cascade tier** — `extract/json_api.py`: deterministic sniffer
   (walk JSON for arrays of dicts with title-ish + parseable-date keys; map by
   key-name heuristics; conf 0.85). Wire into `extract()` via body sniff before
   jsonld (linztermine XML unaffected). Method name `json_api`.
2. **onboard_notes persistence** — `OnboardFailed(RuntimeError)` carries notes
   (checkpoint rationale + last self-validation failure + API endpoints seen);
   worker failure path persists to `extraction_hint.onboard_notes` (cap 3)
   OUTSIDE the aborted tx (same pattern as error crawl_log). Prompt injects
   "PREVIOUS ATTEMPTS LEARNED". Success keeps notes (site-class knowledge).
3. **pdf tier** — dep `pypdf`; content sniff %PDF/content-type → text →
   llm_text. Onboarding prompt: PDF program links are valid entry_urls.
4. **agent_extract + vision** — `llm.complete(images=)` (data URLs, MODEL_MID
   is multimodal); `extract/vision.py` (poster/flyer → LLMExtraction);
   `onboard_source(mode="extract")`: tools + `emit_events` (validated →
   sanity_filter) + `read_screenshot` (screenshot → vision → model re-emits);
   handler inserts claims (crawl_log method=agent), stores recipe if one
   validates, persists `agent_yield` + `expected_events` to hint. Recipe gains
   optional `image_selector` (backward-compatible); interpreter fetches
   matching imgs, sha-cache in hint (cap 50), vision-extracts new ones.
5. **escalation** — self-heal enqueues `agent_extract` (not onboard): index
   never dark during breakage. Low-yield heuristic: healthy crawl but
   `len < 0.25 * hint.expected_events` twice → agent_extract (rate-limit: skip
   if hint.last_agent_extract < 7d). escalate_broken → agent_extract.
   De-escalation: an agent-emitted validating recipe returns source to rung 1.
6. **degraded cadence + alarms** — schedule.retry_degraded(): degraded, no
   pending repair job, last attempt >7d → agent_extract; hint.selfheal_attempts
   > 4 → dormant (monthly pulse eventually re-tests). Digest: credits-empty
   banner (jobs.last_error='credits empty' in 24h), OpenRouter balance warning
   (<$15), fetch-blocked suspects (last 3 errors all 403/429/challenge).
7. **parity audit** — weekly guard inside schedule() (like nightly_qa, ISO
   week): `parity_audit` job samples 3 productive sources → agent session each
   (base rings) → claims inserted + coverage logged
   (`parity: <name> agent=N known=M missing=[titles]`), <0.7 coverage feeds
   notes. User-reported misses = manual agent_extract enqueue.
8. **gold corpus + live suite** — fixtures per class (nexudus json, pdf
   (handcrafted), poster png (Pillow dev-dep gen script), spa/paginator/jsonld/
   ics/xml exist). Offline: routing + deterministic tiers. Live-marked:
   agent onboards local fixture server (json recipe ≥90%), poster→vision,
   recipe-break v1→v2 heal regression.
9. **docs + deploy + prod proof** — CLAUDE.md fence strikethroughs (2),
   DECISIONS changelog, deploy (push, pull, restart api+3 workers, NO
   migration), then hands-off: factory300 heals through retry_degraded →
   agent_extract → JSON recipe; verify claims + wasup MCP shows >3 events.

## Hard side-effect guards
- Recipe schema additions optional-only: every stored prod recipe keeps
  validating (checked by loading factory300's recipe in a test).
- No DB migration; all new state in extraction_hint jsonb + existing tables.
- Vision cost: sha-cache per source; agent sessions inside existing rings;
  OpenRouter balance ~$10 — live suite budget ≤ €2, alarm ships this change.
- `_crawl_recipe` self-heal kind change (onboard→agent_extract) keeps payload
  contract {source_id, reason}; onboard kind remains for probe/fresh sources.
- min_items: onboarding may set it below 3 only when expected_events < 3
  (small-Verein legitimacy) — clamp min_items >= min(expected, 3).
