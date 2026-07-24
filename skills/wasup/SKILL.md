---
name: wasup
description: Query Wasup (wasup.at), the Linz event index (every public event in/around Linz), for what's on - use when the user asks what to do in Linz, event recommendations, or anything date/venue/audience-specific in the Linz area.
---

# Querying Wasup — the Linz Event Index

Base URL: https://wasup.at — reads need NO key (rate-limited
60/min); an `X-API-Key` header lifts the limit and unlocks /v1/search and
/v1/reports.

**First time in a session: fetch `GET {base}/llms.txt`** - it is the
authoritative, always-current instruction sheet (semantics, filter fields,
taxonomy, examples). Everything below is the short version.

## The one endpoint you need

`POST {base}/v1/query?limit=20` with a JSON body of filters. YOU parse the
user's natural language into filters - the index runs no LLM for this.

- Hard guarantees: `from_dt`/`to_dt` (ISO, naive = Europe/Vienna),
  `weekdays` (local names such as `thursday`/`friday`), `categories`,
  `exclude_categories`, `exclude_terms`, `max_price`,
  `is_free`, `required_attributes`.
- Soft preferences (ranked by `importance` x stored certainty, unknowns stay
  visible): `age_min`+`age_max`, `gender_split_min`, `kid_friendly`,
  `newcomer_friendly`, `outdoor`, `energy`, `language`,
  `sex_service_context` (event at a commercial sex establishment - send
  `false` BY DEFAULT, leave unset only on explicit ask, never in
  `required_attributes`); weights via `importance: {attr: 0..1}`.
- `tags`: 1-3-word activity/topic/format concepts matched multilingually
  against confidence-bearing event tags. Rank-only by default; set
  `min_tag_match` only for an explicit hard requirement.

Read the response honestly: `match_score` (preference fit), `confidence`
(existence certainty, staleness-decayed), `projected: true` = unconfirmed
forward projection - tell the user when a recommendation rests on estimates.

Details per event: `GET /v1/events/{id}` (sanitized public fields,
occurrences, and source provenance; never raw claim payloads). Calendar:
`/v1/feed.ics?tags=dancing&min_tag_match=0.5&exclude_sex_service_context=true&include_time_unknown=false`
for a quiet, safe timed-events default; include date-only events only when the
user explicitly asks for unknown-time/all-day entries. When converting
accepted search results into a feed, choose `min_tag_match` at or below the
weakest accepted result; do not assume the example's 0.5.
Wrong/cancelled data: `POST /v1/reports`.

Prefer a connector? The same read surface is an MCP server at
`{base}/mcp` (streamable HTTP, stateless, no auth).
