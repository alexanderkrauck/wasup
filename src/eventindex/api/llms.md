# Wasup — Linz Event Index

Live at: https://wasup.goedly.com (canonical home; wasup.at planned)

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
- **`time_unknown: true`** = the source stated only a DATE; `starts_at` shows
  midnight as a placeholder, not a real time. `ongoing: true` = the
  occurrence started before your window but is still running (exhibitions,
  festivals) - windows use OVERLAP semantics, not starts-only.
- **Geography default**: results are gated to ~15 km around Linz; events
  with UNKNOWN location always pass the gate. Override with `near=lat,lon`
  + `radius=` (then unknown-location events are excluded - it's a hard
  filter); `radius=any` disables the gate.
- **`confidence`** on results decays with staleness (missed re-confirmation
  cycles); `last_confirmed_at` says when a source last showed the event.
- **`provenance_summary`** lists the reporting sources; `GET /v1/events/{id}`
  returns every raw claim per source.

## Querying (use this, it costs the index nothing)

`POST /v1/query?limit=20` - body: any subset of the filter fields (JSON).
Browse-only agent (can only GET)? Same filters as query params:
`GET /v1/query?include_terms=lauf,run&newcomer_friendly=true&importance=newcomer_friendly:1.0&limit=10`
Result-shape params (query string on GET and POST): `sort=starts_at` for
chronological (default `relevance` = match_score x confidence, NOT
chronological!), `distinct=event` for discovery questions ("what guided
tours exist?" - one row per event instead of one per date), `offset=` to
page through the ranked pool (<=2000).
**No API key needed for reads** (query, occurrences, events/{id}, feed.ics,
changes) - anonymous access is rate-limited to 60 req/min per IP; a key
(header `X-API-Key` or `?api_key=`) lifts the limit. Keys are required only
for `/v1/search` (it spends the index's own LLM budget) and `POST
/v1/reports`.

HARD fields (set logic): `from_dt`, `to_dt` (ISO, naive = Europe/Vienna;
a bare date in to_dt means the WHOLE day), `near`+`radius` (geo circle),
`categories`, `exclude_categories`, `exclude_terms`, `include_terms`
(synonym set, at least ONE must appear in title/tags/venue name - use for
"specifically X" queries, e.g. `["lauf","run"]` for running or
`["factory300"]` for events at/by a named venue or organizer;
word-boundary-aware), `max_price`, `is_free`, `required_attributes`.

SOFT preference fields (ranked, never dropped): `age_min`+`age_max`,
`gender_split_min` (0=all male..1=all female), `kid_friendly`,
`newcomer_friendly` (open to strangers vs members-only), `solo_friendly`
(normal to attend alone), `interaction_structure` (built_in = the format
FORCES interaction: rotation/teams/pair work; optional; none = silent
attendance ok), `outdoor`, `energy` (low|medium|high), `language` (de|en).
Optional `importance`: `{attribute: 0..1}` (default 1.0 each).
Attribute names for `importance` and `required_attributes` are: `age` (note:
one name for the age_min/age_max pair), `gender_split_min`, `kid_friendly`,
`newcomer_friendly`, `outdoor`, `solo_friendly`, `interaction_structure`,
`energy`, `language`.

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

Fine print an agent should know:
- Windows use overlap semantics: anything still running at `from` matches
  (flagged `ongoing`); a null `ends_at` is treated as ending at `starts_at`.
- `price_min = 0` means stated-free; `price_min = null` means unknown (the
  `is_free` filter matches only stated-free).
- `match_score` orders results; it is NOT a percentage. Certainties are
  capped (0.8) and unknowns score a 0.45 prior, so an excellent real-world
  fit typically lands around 0.4-0.7. Compare within a result set.
- Rows carry `venue_name`/`venue_address`/`organizer` when known;
  `lat`/`lon` are only set from real venue/claim locations, never guessed.
  `event_status: "tentative"` marks unverified series; `kind: "series"`
  distinguishes recurring events from one-offs. `booking_url` and
  `registration_required` appear when a source stated them.
- Cursors (`next_cursor`) are opaque base64url strings - pass them back
  verbatim. `/v1/occurrences` also takes `include_terms=` for exhaustive
  text listings with cursor paging.

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

## Composition recipes (the power move)

The stored attributes are deliberately neutral primitives; the interesting
queries are COMPOSITIONS you build at query time. Examples:

- "I'm alone and shy but want to meet people" -> `solo_friendly: true` +
  `interaction_structure: "built_in"` + `newcomer_friendly: true` with high
  importance on interaction_structure. The format does the socializing.
- "meet women, going alone" -> the same, plus `gender_split_min: 0.5` with
  high importance. Compose it privately for your user; the index never
  labels anyone's event as a dating venue.
- "where should business X show up / sponsor" -> filter the window, rank by
  audience fit: age/gender/energy matching X's customers, weight by
  `expected_attendance` and confidence from the per-event payloads.

## Other endpoints

- `GET /v1/occurrences?from=&to=&near=lat,lon&radius=5km&category=&min_confidence=&cursor=` - plain listing, keyset-paginated.
- `GET /v1/events/{id}` - full record: field provenance, all claims, all occurrences.
- `GET /v1/feed.ics?...` - any filter combo as a calendar subscription.
- `GET /v1/changes?since=<cursor>` - delta stream over event updates.
- `POST /v1/reports` `{occurrence_id, reason: wrong|cancelled|duplicate, note}` - flag bad data; feeds source trust.
- `GET /v1/search?q=...` - natural-language convenience endpoint (the index
  parses it with its own LLM budget; agents should prefer POST /v1/query).
- `POST /mcp` - MCP server (streamable HTTP, stateless, no auth): the same
  read surface as tools (search_events, get_event, get_calendar_link,
  search, fetch) for MCP clients - ChatGPT apps/connectors, Claude
  connectors. Point your client at https://wasup.goedly.com/mcp
