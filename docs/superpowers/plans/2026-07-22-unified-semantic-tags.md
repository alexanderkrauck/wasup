# Unified confidence-bearing tags

Date: 2026-07-22

## Product contract

- Wasup has one tag system. Source-provided, taxonomy-derived, and LLM-inferred
  tags are rows in the same `event_tag` relation and use the same public
  `{name, confidence, origins}` shape.
- Every tag carries certainty. Exact name equality has semantic relatedness
  `1`; inferred tag certainty remains independent from semantic relatedness.
- A small local multilingual model embeds only normalized 1-3-word tag names.
  No event descriptions, user profiles, or queries are persisted.
- Desired tags rank by default. `min_tag_match` explicitly turns the same tag
  query into a hard requirement; calendar subscriptions use this explicit
  threshold because ranking cannot affect membership in a feed.
- Negative terms remain deterministic set logic. Embeddings never weaken an
  exclusion.

## Minimal architecture

`event_tag(event_id, name, confidence, origins, origin_confidences)` is rebuildable canonical
state. `tag_embedding(name, embedding, model)` is a derived cache. The existing
Postgres and jobs table remain the only state and queue.

The enrichment schema emits richer `tags: [{name, confidence, evidence}]`, a
confidence-bearing language estimate, and explicit-text-only venue and stated
price completion. Canonical source tags and categories are inserted before
enrichment; inferred duplicates merge by maximum confidence and accumulated
origin rather than creating parallel tag types.

`sentence-transformers/paraphrase-multilingual-mpnet-base-v2` runs through
ONNX Runtime using the official quantized AVX-512 model (768 dimensions,
approximately 279 MB). E5-small and E5-base were both measured first; MPNet's
short-phrase ordering was materially better at the same footprint as E5-base.
The repository revision and calibration are pinned. The quantized graph runs
one fixed-shape tag per inference because real-corpus validation showed that
dynamic mixed batches perturb its activation quantization; the tag vocabulary
is small enough that stable vectors matter more than batch throughput. The
model is lazy-loaded, warmed during deployment, and never runs for a request
without tags.

## Matching

For an event tag `t` and desired tag `q`:

1. cosine similarity comes from normalized embeddings;
2. a monotonic sigmoid calibrated on `db/gold/tag_relations.csv` expands the
   model's compressed score range into relatedness `[0, 1]`;
3. exact normalized equality overrides relatedness to `1`;
4. `tag_match = max(tag_confidence * relatedness)` avoids rewarding events
   merely for having many tags.

A monotonic calibration can sharpen separation but cannot repair wrong model
ordering. The gold gate therefore measures both ordering and false matches.
The constants are locked only after the local model passes that gate.

## Surfaces

- `/v1/query` and MCP `search_events`: `tags` plus optional `min_tag_match`.
- `/v1/search`: the existing LLM parser emits the same fields.
- `/v1/feed.ics`: comma-separated `tags` plus `min_tag_match`; semantic
  filtering happens before the feed limit.
- MCP `get_calendar_link`: accepts category, tags, or both and serializes the
  same public feed parameters.
- Event detail returns the one structured tag collection with confidence and
  origins. Raw evidence remains private.

## Rollout and verification

1. Add the compatible tables and code, then remove the unused `event.tags`
   array and `vibe_embedding` column in the same stopped-service deployment.
2. Bump the enrichment schema, re-enrich the real corpus, and embed missing tag
   names through idempotent jobs.
3. Run unit/behavior tests, extraction fixture replays, merge gold set, tag
   relation gold set, and real Linz query/feed examples.
4. Stop all API/workers, pull and sync, migrate, warm the model, start all
   services, queue the rebuild/backfill, then watch job failures, logs, API,
   MCP, and representative semantic searches until stable.
