---
name: eventindex
description: Query the Linz Event Index (every public event in/around Linz) for what's on - use when the user asks what to do in Linz, event recommendations, or anything date/venue/audience-specific in the Linz area.
---

# Querying the Linz Event Index

Base URL: https://wasup.goedly.com — reads need NO key (rate-limited
60/min); an `X-API-Key` header lifts the limit and unlocks /v1/search and
/v1/reports.

**First time in a session: fetch `GET {base}/llms.txt`** - it is the
authoritative, always-current instruction sheet (semantics, filter fields,
taxonomy, examples). Everything below is the short version.

## The one endpoint you need

`POST {base}/v1/query?limit=20` with a JSON body of filters. YOU parse the
user's natural language into filters - the index runs no LLM for this.

- Hard guarantees: `from_dt`/`to_dt` (ISO, naive = Europe/Vienna),
  `categories`, `exclude_categories`, `exclude_terms`, `max_price`,
  `is_free`, `required_attributes`.
- Soft preferences (ranked by `importance` x stored certainty, unknowns stay
  visible): `age_min`+`age_max`, `gender_split_min`, `kid_friendly`,
  `newcomer_friendly`, `outdoor`, `energy`, `language`; weights via
  `importance: {attr: 0..1}`.
- `vibe_terms`: rank-only descriptive words.

Read the response honestly: `match_score` (preference fit), `confidence`
(existence certainty, staleness-decayed), `projected: true` = unconfirmed
forward projection - tell the user when a recommendation rests on estimates.

Details per event: `GET /v1/events/{id}`. Calendar: `/v1/feed.ics?...`.
Wrong/cancelled data: `POST /v1/reports`.

Prefer a connector? The same read surface is an MCP server at
`{base}/mcp` (streamable HTTP, stateless, no auth).
