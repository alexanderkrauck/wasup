# Linz Event Index

An index of every public event in Linz, Austria (and ~25km around): concerts,
theatre, sport courses, Vereine, markets, church fests, gym timetables - the
long tail no portal has. Crawled from ~200+ sources, deduplicated,
confidence-scored. Machine-readable spec: `/openapi.json` (RFC 9727 catalog:
`/.well-known/api-catalog`).

## Semantics you must respect (hard contracts)

- **null means unknown, never "no".** An event without a known category/geo/
  end time is missing data. Hard filters never match unknowns.
- **Exclusions are guarantees.** `exclude_categories`/`exclude_terms` are set
  logic applied BEFORE ranking - an excluded thing cannot appear, period.
- **Every inferred attribute is an estimate with a certainty** (0..0.8 -
  capped by construction; ~0.2 = world-knowledge guess, ~0.35 = typical for
  the category, up to 0.8 with explicit textual evidence).
- **`projected: true`** on an occurrence = a forward-projected repetition of
  an observed weekly/biweekly series (beyond what its source feed shows).
  Treat as "expected, unconfirmed".
- **`confidence`** on results decays with staleness (missed re-confirmation
  cycles); `last_confirmed_at` says when a source last showed the event.
- **`provenance_summary`** lists the reporting sources; `GET /v1/events/{id}`
  returns every raw claim per source.

## Querying (use this, it costs the index nothing)

`POST /v1/query?limit=20` - body: any subset of the filter fields (JSON).
Auth: header `X-API-Key` or query param `api_key`.

HARD fields (set logic): `from_dt`, `to_dt` (ISO, naive = Europe/Vienna),
`categories`, `exclude_categories`, `exclude_terms`, `max_price`, `is_free`,
`required_attributes`.

SOFT preference fields (ranked, never dropped): `age_min`+`age_max`,
`gender_split_min` (0=all male..1=all female), `kid_friendly`,
`newcomer_friendly`, `outdoor`, `energy` (low|medium|high), `language`
(de|en). Optional `importance`: `{attribute: 0..1}` (default 1.0 each).
Attribute names for `importance` and `required_attributes` are: `age` (note:
one name for the age_min/age_max pair), `gender_split_min`, `kid_friendly`,
`newcomer_friendly`, `outdoor`, `energy`, `language`.

Ranking combines **your importance x the stored certainty**, anchored at the
coin flip: an event scores `0.5 + certainty/2` when it satisfies a
preference, `0.5 - certainty/2` when it contradicts it, and `0.45` when the
attribute is unknown - so confident matches rank first, weak guesses beat
unknowns, unknowns beat contradictions, and nothing is silently dropped. The
per-row `match_score` exposes the result. Add an attribute name to
`required_attributes` to make it a hard filter instead (then unknowns are
excluded - use sparingly, most events have estimated attributes only).

`vibe_terms`: free descriptive words ("dance", "cozy") - rank-only, never
filter.

Example - "tonight, no techno, mostly-female crowd matters a lot, kids ok":

```json
POST /v1/query
{"from_dt": "2026-07-08T17:00", "to_dt": "2026-07-08T23:59",
 "exclude_terms": ["techno"],
 "gender_split_min": 0.5, "kid_friendly": true,
 "importance": {"gender_split_min": 1.0, "kid_friendly": 0.4},
 "vibe_terms": ["social", "dancing"]}
```

Taxonomy for `categories`/`exclude_categories`: {categories}

## Other endpoints

- `GET /v1/occurrences?from=&to=&near=lat,lon&radius=5km&category=&min_confidence=&cursor=` - plain listing, keyset-paginated.
- `GET /v1/events/{id}` - full record: field provenance, all claims, all occurrences.
- `GET /v1/feed.ics?...` - any filter combo as a calendar subscription.
- `GET /v1/changes?since=<cursor>` - delta stream over event updates.
- `POST /v1/reports` `{occurrence_id, reason: wrong|cancelled|duplicate, note}` - flag bad data; feeds source trust.
- `GET /v1/search?q=...` - natural-language convenience endpoint (the index
  parses it with its own LLM budget; agents should prefer POST /v1/query).
