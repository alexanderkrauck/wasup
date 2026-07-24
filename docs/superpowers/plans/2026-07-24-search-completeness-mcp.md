# Search, fact completeness, and MCP usability

Date: 2026-07-24

## Product contract

- Discovery is event-first. One event occupies one result slot regardless of
  how many occurrences it has; its next relevant occurrence represents it.
  Chronological occurrence listings and calendar feeds still enumerate dates.
- Structured name lookup is scoped to the event title. `name="ball"` may match
  German compounds such as `Maturaball`; activity, topic, format, atmosphere,
  and audience concepts belong in the single `tags` field. Literal organizer
  and venue names have their own hard filters. A reporting source has its own
  hard `source` filter, so `source="WKO"` composes cleanly with
  `tags=["startup"]` even when the event omitted its organizer.
- Desired tags are a composition, not alternatives. Every desired concept gets
  its own best certainty-weighted semantic match and the concept scores are
  averaged. `min_tag_match` is the explicit hard threshold over that aggregate.
- Public, non-login facts that a human can find are pipeline requirements.
  Missing price, venue, booking link, or confirmed time triggers generic detail
  and public-web recovery. Recovered facts remain append-only claims with the
  exact public URL and evidence.
- Price is one confidence-bearing public attribute. Its `basis` says `stated`
  or `estimated`; hard price guarantees use stated evidence, while soft price
  preferences may use estimates in proportion to confidence.
- Event scale is one confidence-bearing public attribute containing estimated
  participants, a plausible range, a deterministic band, and its basis.
  Internally it completes the existing `expected_attendance` field rather than
  adding a parallel attendance concept.
- PostgreSQL, the existing jobs table, and the existing local multilingual tag
  model remain the only infrastructure. No search service or agent framework is
  added.

## Search contract

The primary discovery call is intentionally compact:

```json
{
  "filters": {
    "name": "ball",
    "from_dt": "2026-07-24T00:00:00+02:00",
    "tags": ["dance", "elegant"],
    "weekdays": ["thursday", "friday"],
    "importance": {"tags": 1.0}
  },
  "sort": "relevance"
}
```

`include_terms` is removed from the structured public contract. The standard
MCP `search`/`fetch` pair remains only because connector hosts require that
document-search contract; both it and `search_events` use the same event-first
candidate core.

For each desired concept `q`:

```text
concept_score(q) =
    max(event_tag_confidence × calibrated_relatedness(q, event_tag))

tag_match = weighted_mean(concept_score(q) for every desired q)
```

The existing monotonic sigmoid remains the nonlinear cosine calibration. It
widens the model's compressed similarity range without changing its ordering.
Exact tag equality remains relatedness 1.

Soft price and scale fields join the existing importance × certainty model.
Hard price uses `max_price`/`is_free`; hard scale uses a stated participant
range plus `required_attributes=["event_scale"]`. Null remains unknown and
never satisfies a hard constraint.

`weekdays` is a hard occurrence filter shared by search and calendar feeds.
It is applied before one occurrence is selected to represent each event, so a
recurring event remains discoverable when its next overall occurrence is on a
different weekday but a later occurrence is on Thursday or Friday.

## Generic fact recovery

The existing time-only detail repair becomes a generic `hydrate_event` job:

1. Fetch the canonical event URL and public booking URL.
2. Extract exact price, venue, booking URL, and confirmed start time from the
   complete readable text with one schema-validated LLM call.
3. Validate quoted evidence against fetched text and validate numeric/URL/time
   sanity deterministically.
4. If required fields remain absent, use the already-budgeted OpenRouter Exa
   retrieval path with event title, date, venue, and organizer, fetch a bounded
   result set, and repeat the same extraction.
5. Append a claim on the event's existing fingerprint with the recovered
   fields, public evidence URL, and raw evidence excerpt.
6. Rebuild canon through the ordinary resolver. Never update exact canonical
   facts directly.
7. Record successful and negative attempts in the existing jobs/crawl log and
   retry unresolved future events on a bounded cadence.

The scheduler prioritizes near-future events, events with booking links, and
events whose captured descriptions mention prices but still lack a canonical
price. Source-level onboarding continues to repair incomplete recipes, but
event hydration guarantees that a large listing's page cap cannot strand a
specific event forever.

## Price and event scale

Enrichment reads the full useful description window and its cache key covers
the same content. Its schema version is bumped.

`price` always returns the best defensible range when applicable:

```json
{
  "min": 39,
  "max": 45,
  "currency": "EUR",
  "confidence": 0.8,
  "basis": "stated"
}
```

An estimate uses `basis="estimated"` and a lower confidence. Only stated
evidence can populate canonical `event.price_min/max`; estimates remain in the
inferred projection and participate only in soft ranking.

`event_scale` has this public shape:

```json
{
  "estimated_participants": 500,
  "plausible_min": 300,
  "plausible_max": 800,
  "band": "large",
  "confidence": 0.35,
  "basis": ["venue capacity", "event format"]
}
```

The LLM must emit a numeric estimate and range. Validation enforces
`1 <= plausible_min <= estimated_participants <= plausible_max`; the public
band is deterministically derived from the numeric estimate.

## MCP contract for calling LLMs

MCP descriptions are executable guidance, not ancillary documentation:

- Server instructions state the tool-selection decision tree and hard/soft
  semantics.
- Every tool description answers what it does, when to use it, what it
  returns, and how to recover from an empty result or validation error.
- `search_events` includes compact positive and negative examples, including
  the ball query, one-call multi-tag composition, and hard-versus-soft price.
- Parameter descriptions say whether null is retained, excluded, or ranked.
- Search results include price, event scale, requested-tag match breakdown,
  provenance summary, and a structured recovery hint when nothing matches.
- The LLM is told not to issue one query per tag and not to call `get_event`
  repeatedly just to compare prices or scale.
- `get_calendar_link` accepts the same filter model. It rejects ranking-only
  preferences that cannot define feed membership and explains how to convert
  them into explicit thresholds. Semantic tag membership therefore requires an
  explicit `min_tag_match`; the validation response tells the caller to choose
  a threshold no higher than the weakest accepted search result.
- The deployed `tools/list` schema is tested directly so connector metadata
  cannot silently drift from the runtime.

## Evaluation gates

### Retrieval

The real-data gold set covers at least:

- `name=ball`, tags `dance` and `elegant`;
- salsa on Friday;
- WKO/startup;
- free family events;
- exclusions;
- recurring versus one-off results;
- hard versus soft price and event-scale constraints.

Release gates: no duplicate events in discovery, zero hard-filter leakage,
all relevant Maturaballs in the requested horizon retrievable in one call,
and no regression of WKO/startup queries.

### Field completeness

A stratified future-event sample records whether exact public prices and other
actionable facts exist outside login walls. A public fact present in the
sample but absent from Wasup is a failed gate. Invented stated prices are a
failed gate. Production metrics include future price coverage,
booking-without-price, unresolved hydration age, and event-scale coverage.

### Agent tool use

An MCP-use gold set maps natural-language tasks to the expected tool, critical
arguments, forbidden calls, and maximum reasonable call count. It covers tool
selection, one-call tag composition, hard/soft intent, details only after
selection, calendar-filter transfer, validation recovery, and correct handling
of price/scale confidence.

After deployment, independent fresh-context agents use the installed live
Wasup MCP for different realistic tasks. Their actual calls and answers are
graded against the same rubric. Any systematic confusion changes the tool
contract and is re-tested before completion.

## Rollout

1. Ship behavior tests before changing search and enrichment.
2. Deploy the event-first query core and the coordinated MCP/REST schema.
3. Restart the API and all three workers together and verify live `tools/list`.
4. Supersede stale enrichment jobs, enqueue one schema-current enrichment per
   event, and enqueue bounded hydration batches.
5. Rebuild canon, embed new tags, and watch jobs, spend, failures, price
   coverage, scale coverage, API latency, and the real query gates.
6. Run independent MCP agents, repair any observed failure, redeploy, and
   repeat until every gate passes.

## Implementation and acceptance notes

The implementation shipped as five coordinated production increments:
`9b8fdac`, `9cfd00f`, `d98395e`, `944dacf`, and `fa45f7c`.

- The deployed MCP schema exposes `name`, unified `tags`, `weekdays`, hard and
  soft price/scale controls, and the same filter object for calendar links.
  The removed `vibe_terms` concept was not retained as a compatibility alias.
- A real Friday salsa task exposed the missing weekday control and misleading
  point-like low-confidence attendance ranges. The generic weekday filter and
  confidence-scaled public range rendering were added and deployed.
- A real WKO/startup task exposed that a calendar silently choosing the old
  default semantic threshold could omit accepted search results. Calendar link
  generation now requires the caller to transfer an explicit threshold.
- Deployment monitoring exposed the OpenAI SDK's hidden two-retry layer and
  600-second default read timeout beneath Wasup's own retries. The shared LLM
  client now uses one explicit 90-second timeout, disables SDK retries, and
  keeps the existing schema-validation-aware retry loop as the sole policy.
- Live generic hydration recovered exact public prices with quoted evidence for
  the Debütantenball Wels and other unrelated event types. For the current
  Freistadt Maturaball it found only prior-year or otherwise unsupported public
  price evidence and correctly refused to label that as a current stated
  price; enrichment remains responsible for a confidence-labeled estimate.
- The first independent agent using the installed Codex connector saw the
  connector metadata cached when this task started. Direct calls to the
  authoritative live MCP endpoint saw the new schema immediately. Refreshing
  that host-side connector cache is an app lifecycle concern, not grounds for a
  second legacy tag API in Wasup.
