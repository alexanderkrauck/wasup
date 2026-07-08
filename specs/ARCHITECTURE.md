# Linz Event Index - Architecture & Core Routine

Goal: index **every** event happening in/around Linz - from Brucknerhaus concerts down to the spinning class at a Bindermichl gym, the Verein Stammtisch, and the Pfarre flea market. Coverage of the long tail is the product. Everything else (API, filters, semantics) is table stakes.

Design principles:

1. **Source registry is the crown jewel.** The tech is replicable; the curated, scored, ever-growing list of Linz event emitters is not.
2. **Claims vs. canon.** Never overwrite. Every source produces *claims* about events; a resolver merges claims into one canonical event with per-field provenance and confidence.
3. **LLM at the edges, deterministic in the middle.** LLMs do extraction and attribute inference. Scheduling, dedup keys, recurrence expansion, and merging are deterministic and testable.
4. **Adaptive everything.** Crawl frequency, source trust, and extraction strategy all adjust automatically from observed yield and accuracy.
5. **Never delete, always version.** Events get corrected/cancelled constantly. Keep claim history so you can debug why the canon says what it says.

---

## 1. System Overview

```
┌────────────────────────────────────────────────────────────────────┐
│  DISCOVERY          CRAWL            EXTRACT         RESOLVE       │
│                                                                    │
│  GMaps/OSM ─┐   ┌─ scheduler ─┐   ┌─ JSON-LD/ICS ┐   ┌─ venue    │
│  Portals ───┼─▶ │ source      │─▶ │ RSS/APIs     │─▶ │   resolver │
│  Link graph─┤   │ registry    │   │ LLM extract  │   │ event      │
│  Search ────┤   │ (tiered)    │   │ OCR (flyers) │   │   dedup    │
│  Socials ───┘   └─ fetchers ──┘   └─ recurrence ─┘   └─ merger ──┐│
│                                                                   ││
│  ENRICH                    STORE                    SERVE         ││
│  geocode, taxonomy,   ◀──  Postgres + PostGIS  ──▶  REST API  ◀──┘│
│  demographics/fullness     + pgvector               semantic search│
│  inference, embeddings     (claims + canon)         webhooks/ICS   │
└────────────────────────────────────────────────────────────────────┘
```

Everything runs as one scheduled pipeline (cron / temporal-style workflow), plus an always-on API.

---

## 2. Data Model

Five core tables. Postgres + PostGIS + pgvector.

### `source`
The registry. One row per crawlable origin (a website, an Instagram account, an ICS feed, a portal category page).

```sql
source (
  id              uuid PK,
  name            text,
  url             text,
  kind            text,      -- website | ics | rss | api | instagram | facebook | portal | newsletter | pdf_page
  entity_type     text,      -- venue | gym | verein | church | university | promoter | portal | ...
  venue_id        uuid NULL, -- if source belongs to a known venue
  geo             geometry NULL,
  tier            int,       -- 1=official APIs/portals, 2=structured sites, 3=unstructured sites, 4=socials/OCR
  trust           float,     -- 0..1, learned (see §7)
  crawl_interval  interval,  -- adaptive
  last_crawled    timestamptz,
  last_yield      int,       -- events found last crawl
  yield_ema       float,     -- moving avg, drives scheduling
  extraction_hint jsonb,     -- e.g. {"events_path": "/kurse", "format": "pdf_timetable", "lang": "de"}
  discovered_via  text,      -- gmaps | link_graph | search | manual | portal_backlink
  status          text       -- active | dormant | dead | blocked
)
```

### `event_claim`
Raw, per-source assertion. Append-only.

```sql
event_claim (
  id              uuid PK,
  source_id       uuid FK,
  crawl_id        uuid,
  fingerprint     text,      -- dedup key, see §6
  raw_excerpt     text,      -- the text/HTML the claim came from (debuggability!)
  payload         jsonb,     -- extracted fields, each with {value, confidence}
  extracted_at    timestamptz
)
```

### `event` (canonical)
```sql
event (
  id              uuid PK,
  kind            text,      -- one_off | series | course | festival | standing_offering
                             -- (standing_offering = museum open hours, drop-in sauna, etc.
                             --  users don't distinguish "event" from "thing I can do")
  parent_event_id uuid NULL, -- sub-events: festival acts, conference talks
  title           text,
  description     text,
  rights          text,      -- quoted (scraped, don't republish) | generated (ours) | licensed
  category        text[],    -- taxonomy, see §8
  tags            text[],
  venue_id        uuid FK,
  geo             geometry,
  is_recurring    bool,
  rrule           text NULL, -- RFC 5545 recurrence
  -- logistics (these decide whether a user can ACT on a listing):
  registration_required bool NULL,
  registration_deadline timestamptz NULL,
  booking_url     text NULL,
  drop_in_ok      bool NULL,
  doors_at_offset interval NULL,  -- doors vs. start
  late_entry_ok   bool NULL,
  participation_mode text NULL,   -- spectate | participate | both
  price_min/max   numeric,
  url             text,      -- best public link
  image_url       text,
  lang            text,
  -- inferred attributes, each mirrored by *_confidence float:
  expected_age_range      int4range,
  expected_gender_split   float,   -- 0=all male .. 1=all female, 0.5=balanced
  expected_attendance     int,
  expected_fullness       float,   -- 0..1 vs venue capacity
  vibe_embedding          vector(1024),
  field_provenance jsonb,   -- {"title": {"claim": uuid, "confidence": 0.97}, ...}
  confidence       float,   -- overall "this event is real and correct"
  status           text,    -- confirmed | tentative | cancelled | past
  first_seen / last_seen / updated_at timestamptz
)
```

### `occurrence`
Recurrence expanded into concrete instances (what the API actually serves for time queries).

```sql
occurrence (
  id          uuid PK,
  event_id    uuid FK,
  starts_at   timestamptz,
  ends_at     timestamptz NULL,
  status      text,        -- scheduled | cancelled | moved | postponed_tba
  availability text NULL,  -- available | limited | waitlist | full | unknown
                           -- "full" covers sold_out AND ausgebucht/0-slots-in-booking-widget:
                           -- practical fullness, not just ticket sales. Signals: ticket-tier
                           -- depletion, "ausgebucht"/"Warteliste", booking-widget slot counts,
                           -- registration-closed. Free events: usually unknown (honest null).
  waitlist_url text NULL,
  fullness_estimate float NULL,   -- soft estimate (§8); availability is the hard signal
  last_confirmed_at timestamptz
)
```

### `venue`
```sql
venue (
  id          uuid PK,
  name        text,
  aliases     text[],      -- "Posthof" = "Posthof Linz" = "Posthof - Zeitkultur am Hafen"
  address     text,
  geo         geometry,
  capacity    int NULL,
  gmaps_place_id text,
  kind        text         -- concert_hall | gym | bar | church | park | ...
)
```

---

## 3. The Core Routine

The whole system is one loop, run continuously (each stage is an independent worker pulling from a queue):

```
every 15 min:
  1. SCHEDULE   pick sources where now > last_crawled + crawl_interval,
                ordered by (yield_ema × staleness × tier_priority)
  2. FETCH      per source kind:
                  http GET → if JS-shell detected → headless render
                  ics/rss/api → direct
                  instagram/facebook → scraper/API
                skip if content_hash unchanged since last crawl → cheap early exit
  3. EXTRACT    cascade (stop at first success):
                  a. JSON-LD schema.org/Event          (free, precise)
                  b. ICS / RSS / platform API payload  (free, precise)
                  c. LLM extraction on readable text   (cheap model, structured output)
                  d. Vision-LLM on images/PDF flyers   (only if page signals events but text yields none)
                → emit event_claims with per-field confidence
  4. RESOLVE    fingerprint → match against existing claims/events (§6)
                merge into canonical event, field-by-field, trust-weighted (§7)
  5. ENRICH     geocode, categorize, expand rrule → occurrences,
                infer demographic/fullness attributes (§8), embed
  6. MAINTAIN   nightly:
                  - mark past occurrences, expire dead events
                  - source health: adjust crawl_interval and trust
                  - discovery jobs (§4) add new sources
                  - QA sample: re-check N random events against their source URL
```

Adaptive recrawl (step 1) is the cost lever:

```
crawl_interval ← clamp(
    base[tier] × (1 / (1 + yield_ema)) × change_factor,
    min=6h, max=21d
)
```

A nightclub posting weekly gets crawled daily; a Verein that posts twice a year drifts toward every 2-3 weeks; a dead page goes dormant (but never fully deleted - it gets a monthly pulse check). Additionally: **event-proximity boost** - any source with a known occurrence in the next 72h gets recrawled daily regardless, to catch cancellations and sold-out status.

---

## 4. Automated Source Discovery

Discovery is its own pipeline, run weekly. Target: the registry grows and self-heals without manual work.

**a. Places sweep (the backbone).** Google Places API + OSM (Overpass) for the Linz metro area (Linz + Leonding, Traun, Ansfelden, Enns, Urfahr surroundings, ~25km radius). Pull every place in event-emitting categories: `night_club, bar, gym, stadium, church, museum, art_gallery, university, community_center, dance_school, bowling_alley, library, theater, casino, tourist_attraction, restaurant(with events page), ...`. Each place → website URL → probe for event signals (`/events`, `/veranstaltungen`, `/termine`, `/kurse`, `/programm`, sitemap scan, ICS link, JSON-LD presence). Signal found → register source. OSM adds what GMaps misses: `club=*`, `amenity=community_centre`, `leisure=sports_centre`, Vereinsheime.

**b. Portal ingestion + backlink mining.** Ingest linztermine.at, eventfinder.at, meinbezirk.at, oeticket, linz.at, linztourismus.at, eventbrite, meetup as tier-1 sources. But also mine them: every organizer/venue link inside a portal listing is a candidate *direct* source (the portal shows 10% of what the organizer's own site has).

**c. Link-graph expansion.** Venue sites link to co-hosts, promoters, partner Vereine. Crawl outlinks from every registered source's event pages, score candidates ("does this domain look Linz-local + event-emitting?" - cheap LLM call), auto-register above threshold, queue borderline ones for manual review.

**d. Search fan-out.** Monthly programmatic search matrix: `{category} × {Linz, Urfahr, Leonding, ...} × {termine, kurse, veranstaltung, workshop, treffen}`. Categories from the taxonomy (§8): yoga, klettern, salsa, schach, lesung, flohmarkt, repair café, brettspiele, ruderverein... Any result domain not in registry → probe → register. This is the main net for the niche stuff.

**e. Registry sweeps (Austria-specific gold):**
- **Vereinsregister / ZVR** - every registered Verein in Linz. Vereine are the densest niche-event emitters and most have some web presence.
- **Sport umbrella orgs**: ASKÖ OÖ, SPORTUNION OÖ, ASVÖ - club directories with hundreds of member clubs.
- **Diözese Linz** - parish directory; Pfarren run concerts, markets, Feste.
- **WKO firm directory** - dance schools, VHS-like course providers.
- **VHS OÖ / Volkshochschule Linz course catalog** - hundreds of courses per semester, one source.
- **JKU + Kunstuni + FH OÖ + ÖH** event calendars, StV/Institut pages.

**f. Socials sweep.** For every venue/Verein in the registry, resolve their Instagram/Facebook handle (usually linked from their site or GMaps profile) and register it as a *secondary* source (tier 4). Many small organizers post events ONLY on Instagram. Facebook Events for the region via scraper. Also: r/Linz, local Discord/Telegram groups (manual seed list), Bandsintown/Songkick for touring acts.

**g. Zero-result monitor.** Track semantic search queries from the API that return nothing ("aerial yoga linz"). Recurring misses trigger a targeted search-fan-out job. Users tell you where your coverage holes are.

---

## 5. Fetching Notes

- Respect robots.txt on tier 2-3 sites; identify with an honest UA + contact address.
- Static fetch first; escalate to headless (Playwright) only when the DOM is a JS shell. Cache the decision in `extraction_hint`.
- Content-hash early exit: ~70-80% of crawls end here, costing nothing but the GET.
- PDFs (course timetables, Pfarrblätter, season programs) go to the vision-extraction path. Season program PDFs are high-yield: one document = 50 events.
- Instagram/FB via scraping APIs (Apify or similar) - budget for it; it is the only channel for a real chunk of the long tail.

---

## 5b. Recipe-Based Universal Crawler (no per-site code)

Rule: **humans write zero per-site crawlers.** There are exactly two hand-built fetch engines - (1) generic web (HTTP + optional headless) and (2) socials (Instagram/FB via scraping API). Everything site-specific lives in a **recipe**: declarative data, synthesized by an AI onboarding agent, executed by one generic interpreter.

### The recipe

```jsonc
// source.recipe (jsonb) - synthesized, versioned, regenerable
{
  "version": 3,
  "entry_urls": ["https://www.wko.at/ooe/veranstaltungen?page={n}"],
  "render": "http",                      // http | headless
  "pagination": {                        // pagination is a small closed set:
    "type": "url_param",                 // url_param | next_link | load_more_click |
    "param": "page", "start": 1,        // infinite_scroll | calendar_nav | date_range_param |
    "max_pages": 30                      // form_post | none
  },
  "item_scope": "css:div.event-card",    // optional: narrows what the extractor sees
  "field_selectors": {                   // optional: if stable, extraction becomes free
    "title": "css:h3 a", "date": "css:.date", "detail_url": "css:h3 a@href"
  },
  "follow_detail": true,                 // fetch each item's detail page for full text
  "stop_conditions": ["date_older_than_now", "all_fingerprints_seen"],
  "validation": {                        // the contract that detects breakage
    "min_items": 3,
    "required_fields": ["title", "date"],
    "date_parse_rate": 0.9
  }
}
```

The interpreter understands ~8 pagination types, which cover ~95% of the web (numbered pages, next links, load-more buttons, infinite scroll, month-by-month calendars, date-range query params, POST search forms). That closed set is what keeps the interpreter small and testable - the *variability* lives in the recipe, not in code.

### Recipe synthesis (the onboarding agent)

When discovery registers a new source, an agent session (browser tools + LLM) runs once:

1. Load the site, locate the event/course/termine listing (it already has the probe hint from discovery).
2. Classify pagination by interacting: click "weiter", scroll, flip the calendar month - observe URL/DOM changes.
3. Propose `field_selectors` by diffing 3-5 item nodes; if the DOM is too messy/unstable, omit them (extraction falls back to LLM-on-page-text - costs more per crawl but always works).
4. Emit the recipe, then **immediately validate it**: the interpreter executes it fresh; extracted events are checked against what the agent saw. Pass → recipe goes live. Fail → one retry, then flag for the manual-review queue.

Cost: one agent session (~cents) per source, once. WKO, VHS, Diözese, meinbezirk, a gym's course table - all just recipes, no code.

### Self-healing

Recipes rot when sites redesign. Detection is the `validation` contract plus yield monitoring:

```
after each crawl:
  if validation fails OR yield drops >80% vs yield_ema for 2 consecutive crawls:
    source.status = degraded
    → re-run onboarding agent (it gets the old recipe + failure reason as context)
    → new recipe version; old one kept for diffing
  if regeneration fails twice → manual queue (in practice: a handful/month)
```

Selector-free recipes (pure LLM extraction) barely ever break - so the system degrades gracefully: selectors are an *optimization* the agent adds when the DOM allows it, never a dependency.

### Code escape hatch (rare)

Maybe 2-5% of sources are too weird for the declarative set (ASP.NET viewstate forms, auth-gated widgets, exotic JS calendars). For those the agent may emit `recipe.type = "code"`: a short Python function implementing a fixed interface (`fetch(source, state) -> [raw_items]`), stored in the DB, executed in a sandbox with network+time limits, versioned and regenerated exactly like declarative recipes. Same self-healing loop. Human review required before first activation of any code recipe - that's the one manual gate.

### Tier 3: full agent crawl (the last resort)

Some sources defeat both recipes and generated code - SPA calendars with unstable DOMs, sites where events hide behind multi-step interactions, pages that restructure constantly. For those, the crawl itself is an agent session: browser tools + goal prompt ("extract all upcoming events from this site into this schema"), no recipe at all.

Execution ladder per source:

```
tier A: recipe (declarative)      ~€0.001-0.01 / crawl
tier B: generated code            ~€0.001-0.01 / crawl
tier C: LLM-extract full text     ~€0.01-0.05  / crawl
tier D: full agent session        ~€0.10-0.50  / crawl   ← hard-capped
```

Sources escalate only on repeated failure of the tier below, and every tier-D run tries to **distill itself back down**: the agent's successful trajectory (URLs visited, actions taken, where the data lived) is fed to the recipe synthesizer as a worked example. Most sources visit tier D a few times, then graduate to a recipe. Tier D as a *permanent* home should be <1% of the registry.

### Cost governance (upper-bounding the weird)

Three nested limits, all enforced, all in the DB:

1. **Per-crawl hard caps** (all tiers): max pages, max detail fetches, max headless seconds, max LLM tokens, max agent turns (tier D: e.g. 25 turns / €0.50, then the session is killed and yields whatever it found).
2. **Per-source monthly budget**: every source has `monthly_budget_eur` (default by tier; tier D maybe €3/month). Exhausted → source skips crawls until the month resets. Budget spend is logged per crawl, so cost per source is always queryable.
3. **Global envelope**: a system-wide daily spend ceiling. The scheduler orders work by expected value, so if the envelope tightens, the least valuable crawls fall off first - the system degrades by crawling less, never by surprise bills.

The scheduler's value ordering is the key mechanism - each source carries:

```
value_score = yield_ema × uniqueness × freshness_need
uniqueness  = share of its events found on NO other source   (long-tail gyms score ~1.0,
                                                              portals score low)
cost_score  = ema of € per crawl
priority    = value_score / cost_score
```

So the answer to "this site is too weird" is almost never *drop it* - it's *crawl it monthly instead of daily*. A €0.40 agent crawl of a source that uniquely yields 10 niche events is excellent economics at monthly frequency and terrible at daily; the priority formula makes that call automatically. Sources whose priority stays under a floor for 60 days get parked (status `dormant`, quarterly pulse check) with a note in the manual-review queue - the human decision is only ever about the extreme tail.

One more escape valve for genuinely hostile-but-valuable sources: `acquisition = manual` - no crawler at all; the source appears in a weekly human checklist (or you just email the organizer asking for their program - small Linz organizers will happily send you their season PDF). The system tracks it like any other source; only the fetch step is human.

### Agent engine (harness requirements)

Both agentic components - the onboarding agent (§5b) and tier-D crawls - run on one shared harness. Requirements for whatever harness is used:

1. **Tool interface**: browser tools (navigate, click, scroll, read DOM/text, screenshot for vision fallback) + a `emit_events(json)` tool that validates against the event schema inline - the agent submits as it goes, so a killed session keeps partial yield.
2. **Budget enforcement inside the loop**: max turns, max tokens, wall-clock timeout - enforced by the harness, not trusted to the model. On budget exhaustion: graceful stop, return partial results + state.
3. **Trajectory logging**: full record of (page, action, observation) per turn - this is the raw material for recipe distillation, non-negotiable.
4. **Model pluggability per session**: the caller picks the model; the harness doesn't hardcode one.
5. **Sandboxing**: no filesystem/network beyond the browser; tier-D sessions touch untrusted web content, treat all page text as adversarial (prompt-injection: the agent's only write-capable tool is `emit_events`, so the blast radius of an injected instruction is bounded to bad event data - which the validation + confidence layer already handles).

### Model routing (cheap by default)

Default everywhere is a mini model; escalation is failure-driven, never guessed upfront:

```
task                        default          escalate to        trigger
──────────────────────────────────────────────────────────────────────────
field extraction (text)     mini             mid                validation contract fails
                                                                (dates don't parse, fields missing)
classification/taxonomy     mini             -                  never (errors are cheap)
recipe synthesis            mini             mid → frontier     recipe fails own validation twice
merge adjudication          mini             mid                confidence in grey zone twice
enrichment (demographics)   mini             -                  never (estimates anyway)
tier-D agent crawl          mini             mid → frontier     session ends with 0 events
                                                                or coherence check fails
vision (flyers/PDFs)        mini-vision      mid-vision         date_parse_rate < 0.9
```

Two mechanics make cheap-first safe:

- **Validation is the safety net, not model quality.** Every output passes deterministic checks (dates parse, geo in metro, schema valid, cross-source agreement). A mini model that fails loudly costs one retry at the next tier; it never silently poisons the index.
- **Sticky routing with decay**: if a source needed the mid model 3× in a row, pin it there (`source.model_tier`), but retry mini every ~10 crawls - models get cheaper/better, sites get re-onboarded, pins shouldn't be forever.

Expected mix in steady state: >90% of all LLM calls on mini models. The frontier model appears only in recipe synthesis for nasty sites and a handful of tier-D crawls - single-digit calls per day.

---

## 6. Dedup / Entity Resolution

Two-stage: cheap blocking, then careful matching.

**Fingerprint (blocking key):** `normalize(title) + date_bucket + geo_cell`
- title normalized: lowercase, strip venue names/dates/stopwords, German umlaut folding
- date bucket: calendar day of start
- geo cell: ~500m grid (or venue_id if resolved)

Claims sharing a fingerprint are candidate duplicates. Then pairwise match score:

```
score = 0.35 × title_similarity(trigram + word containment; embedding cosine
        deferred with OPEN-QUESTIONS #9 - as built 2026-07-05/07)
      + 0.25 × time_overlap
      + 0.20 × venue_match (resolved venue > geo distance)
      + 0.10 × organizer_match
      + 0.10 × url/image overlap
→ score > 0.80 auto-merge | 0.55-0.80 LLM adjudication ("same event?") | below: distinct
```

**Venue resolution first, always.** Most dedup errors are venue-alias errors. Maintain the `venue.aliases` list aggressively; every new venue string gets fuzzy-matched against existing venues before creating a new one.

**Recurrence dedup:** "Spinning Di 18:00" from a gym timetable must merge with itself week after week - match at the *event* (series) level via rrule + title + venue, not per occurrence. A series is one event row; the crawler updates its rrule/validity window rather than creating new rows.

**Cross-source merging:** when the same concert appears on oeticket, the venue site, and Instagram, keep one canonical event; per field, pick the claim with highest `source.trust × field_confidence`. Ticket platforms win on price/status; the venue site wins on description; the portal wins on nothing but confirms existence (which *raises* overall event confidence).

**Exception - status/availability fields merge asymmetrically, recency-first.** Trust-weighted voting is wrong for `status` and fullness: if the venue posts "ABGESAGT" while three portals still show the stale listing, the portals are not confirming - they just haven't updated. Rules:

- A *negative* claim (cancelled / moved / sold_out / ausgebucht) from any source with trust > 0.5 wins immediately over all *older* positive claims, regardless of their count or trust. Stale "scheduled" is silence, not evidence.
- A negative status is only reverted by a *newer* positive claim from an equal-or-higher-trust source (e.g. venue un-cancels, tickets re-released).
- Every negative claim triggers an immediate re-crawl of the event's other sources (confirmation sweep) - usually turning a 1-source cancellation into a multi-source one within hours.
- `last_confirmed_at` per occurrence always reflects the newest claim of any polarity, so consumers see exactly how fresh the state is.

---

## 7. Confidence Model

Three layers, all 0-1 floats:

1. **Source trust** (per source, learned):
   - starts from tier prior (API/portal 0.9, structured site 0.8, LLM-extracted site 0.65, socials/OCR 0.5)
   - updated by outcomes: QA spot checks, cross-source agreement rate, cancellation surprises (event we said was on, but venue said cancelled), user reports
   - `trust ← 0.9 × trust + 0.1 × observed_accuracy` (slow EMA)

2. **Field confidence** (per extracted field, from the extractor):
   - JSON-LD/ICS fields: 0.95+
   - LLM-extracted: model self-reports + calibration layer (validate: does the date parse? is the geo inside the metro? does price look sane?)
   - inferred attributes (§8) are capped at 0.8 - they are estimates by construction

3. **Event confidence** (canonical): `1 - Π(1 - trustᵢ × confᵢ)` over supporting claims - independent confirmations compound. One Instagram post = 0.5; Instagram + venue website = 0.85; + oeticket = 0.95.

The API exposes all three. Filters accept `min_confidence`; the default feed hides < 0.4 but a `?include_tentative=true` shows everything - for the niche-hunting use case you often WANT the 0.45-confidence hint that something might be happening.

**Staleness decay:** canon confidence also decays passively. Every event has an expected re-confirmation cadence (derived from its sources' crawl intervals); each missed cadence multiplies confidence by ~0.9. A Stammtisch last confirmed by a 2019 website drifts to "tentative" on its own - zombie listings die of old age instead of living forever. Any fresh confirmation resets the decay.

**Decay is computed at query time, not by a batch job:** effective confidence = `stored_confidence × decay(now - last_confirmed_at)`, evaluated in the API layer. This makes honesty independent of the pipeline being alive: if crawling halts entirely (server down, key expired), events fade out of the default feed automatically instead of being served at frozen confidence forever. A fully dead pipeline converges to an empty feed, never a confidently stale one. Complementary: a dead-man's switch (no successful crawl in 48h → alert) and a `data_freshness` field in every API response (timestamp of the newest successful crawl) so consumers can detect a stalled index themselves.

**Unknown means unknown (API contract):** every nullable field's `null` means "not known", never "no". `ends_at=null` must never be read as "ends whenever you need it to". Consumers can require knowledge (`ends_before=20:00` only matches events with a known end). This is stated in the API docs as a hard semantic guarantee.

**Negative constraints are set logic, not similarity:** exclusion filters (`exclude_category=comedy`) are exact tag/category filters applied BEFORE ranking; embeddings only rank within the allowed set. A grieving user who says "nothing funny" gets a guarantee, not a probability.

---

## 8. Enrichment & Inferred Attributes

**Taxonomy:** two-level, ~15 top / ~120 sub (music>techno, sport>climbing, community>flohmarkt, learning>language_exchange, family>kids_workshop, ...). Assigned by LLM at extraction, embeddings as fallback. Events can hold multiple categories.

**Inferred audience attributes** - computed by an LLM enrichment pass over (event text + venue profile + category priors), each with confidence:

| attribute | signal sources |
|---|---|
| `expected_age_range` | category priors, wording ("Studentenparty" vs "Seniorencafé"), venue history, start time |
| `expected_gender_split` | category priors (base rates per category/subculture), organizer audience, past attendee signals where visible |
| `expected_attendance` | venue capacity × category prior × artist/organizer draw (follower counts, past RSVP data from Meetup/FB where visible) |
| `expected_fullness` | expected_attendance / capacity; boosted by sold-out signals, ticket-tier depletion, "nur noch Restkarten" |
| `vibe` (embedding + tags) | free-text: "sweaty basement techno" vs "sit-down jazz brunch" - powers semantic search |
| `price_band`, `is_free` | extracted or inferred |
| `language` | de / en / other - matters in a student city |
| `weather_dependence` | outdoor flag → API can join a weather forecast |
| `accessibility`, `kid_friendly`, `newcomer_friendly` | wording + venue attributes; newcomer_friendly (open to strangers vs. members-only Verein evening) is a killer filter for exactly your "I don't know 1000 people" problem |

Priors live in a small editable table (`category_priors`), get overridden by explicit evidence, and can later be calibrated against reality (attendance feedback, check-in data if the product grows a community).

**Recurrence normalization** deserves emphasis: half the long tail is "jeden Dienstag 18:30 außer Ferien". Extraction maps this to RRULE + EXDATE (Austrian school holidays / feiertage table built in), expanded 8 weeks ahead into `occurrence` rows.

---

## 9. API

REST, JSON, versioned (`/v1`). Keyset pagination. API-key auth.

```
GET /v1/occurrences
    ?from=2026-07-03T18:00&to=2026-07-06
    &near=48.3069,14.2858&radius=5km          (or &bbox=)
    &category=sport.climbing,community.*
    &price_max=15&is_free=true
    &age_range=20-35&max_fullness=0.7&newcomer_friendly=true
    &min_confidence=0.6&include_tentative=true
    &sort=starts_at|distance|confidence|relevance
    &q=free text                              → hybrid: filters + pgvector semantic
                                              (superseded 2026-07-06: see /v1/search note below)

GET /v1/events/{id}            full record incl. field_provenance + claims
GET /v1/events/{id}/similar    embedding neighbors
GET /v1/venues, /v1/venues/{id}/events
GET /v1/search?q=...           AGENT search, not pure semantic (redefined by Alexander
                               2026-07-06, DECISIONS changelog): a mini model parses the
                               query into HARD filters (time, category, exclusions,
                               audience, price - set logic in SQL); residual vibe terms
                               only RANK within the allowed set. Embeddings never select.
GET /v1/feed.ics?{same filters}   any filter combo as a calendar subscription
POST /v1/reports               user feedback: wrong/cancelled/duplicate → QA queue,
                               feeds source trust
GET /v1/changes?since=cursor   delta stream for downstream consumers/agents
```

Response objects carry `confidence`, `provenance_summary` (["venue_website","oeticket"]), and every inferred attribute with its confidence - so a consumer (human UI or an AI agent) can decide what to trust. The `/changes` endpoint plus `.ics` filters means the index is also directly usable as "my personal Linz radar" without building a frontend first.

---

## 9b. Privacy, Takedown & Scope Honesty

"We index everything public" collides with "you shouldn't have." Non-negotiables:

- **Private-intent suppression**: heuristics at resolve time - residential address + small/no venue + personal-profile organizer → suppress address/geo (publish district-level only) or hold the event entirely for review. Technically-public ≠ intended-public.
- **Takedown**: `POST /v1/takedown` (organizer self-service) with a 48h SLA; takedowns also add the source pattern to a suppression list so recrawls don't resurrect the listing. GDPR: organizer names/addresses are personal data - deletion requests are honored in claims history too (redaction, since claims are append-only).
- **Scope honesty over fabrication**: the index knows events, not safety ratings, not vendor menus, not parking. The API never fills these gaps with guesses - out-of-scope questions get explicit "unknown" (see §7), and the consumer-facing layer should say so ("no info on gluten-free stalls - here's the organizer contact").

---

## 10. Linz Source Catalog - the Don't-Miss List

Seed registry, by tier. (Discovery expands from here; this is the manual bootstrap.)

**Tier 1 - portals & platforms (ingest + mine for backlinks):** linztermine.at, eventfinder.at, meinbezirk.at/linz, linz.at events, linztourismus.at, oeticket, eventbrite, meetup.com, eventpicker.at, Bandsintown/Songkick (Linz), Facebook Events (region scrape), kupfticket.

**Tier 2 - institutional calendars:** Brucknerhaus, Musiktheater/Landestheater, Posthof, Ars Electronica Center, Lentos/Nordico, OK/OÖ Kulturquartier, Stadtwerkstatt, Kapu, Ann and Pat, Röda (Steyr), Tabakfabrik events, Design Center, TipsArena, Raiffeisen Arena/LASK, Black Wings, JKU + ÖH JKU, Kunstuni, FH OÖ, VHS Linz (course catalog!), Stadtbibliothek/Wissensturm, Botanischer Garten, Zoo Linz, AEC Deep Space program, Moviemento/City-Kino, Central, Diözese Linz event page.

**Tier 3 - the long tail (this is the moat):** every gym & fitness studio (spinning/yoga/crossfit schedules), climbing halls (Auf der Gugl, boulder gyms), dance schools (Horn, salsa/tango/swing communities), martial arts dojos, sports clubs via ASKÖ/Union/ASVÖ directories, rowing/sailing clubs at the Donau, chess/board game/TCG stores & clubs, maker spaces (Grand Garage), repair cafés, Pfarren (concerts, markets, Feste), Kleingartenvereine, volunteer orgs, language exchange meetups, expat groups, student fraternities' public events, senior centers, Eltern-Kind-Zentren, libraries' Vorlesestunden, Bauernmärkte/flea markets, Urfahraner Markt, pop-up announcements via city PR.

**Tier 4 - socials & fuzzy:** Instagram accounts of ALL of the above (many post only there), promoter accounts, r/Linz, local Telegram/Discord/WhatsApp community groups (seeded manually), club residents' accounts, university StV Instagram pages.

Rough expectation: 800-1500 registered sources for the metro area, yielding (estimate) 300-600 distinct events/week, of which maybe half appear on no portal.

---

## 11. Stack & Cost Sketch

- **DB:** Postgres + PostGIS + pgvector. One box. This never needs to be "big data" - Linz is maybe 30-50k occurrences/year.
- **Workers:** Python (httpx, Playwright, icalendar, dateutil.rrule), queue via Postgres SKIP LOCKED (*Redis option removed - CLAUDE.md forbids it; as built: Postgres only*). No Kafka, no k8s.
- **LLM:** mini model for extraction/classification, mid model for adjudication escalation + agentic onboarding, vision when the PDF fence unfences. Structured outputs everywhere. (*As decided 2026-07-07: mini=deepseek-v4-flash, mid=kimi-k2.7-code, two tiers only - see DECISIONS changelog.*)
- **Scrapers:** Apify (or similar) for Instagram/Facebook - the one line item worth paying for (*when the socials fence unfences, H7.2*).
- **API:** FastAPI. No caching until a measured query is slow (CLAUDE.md).

Cost estimate at steady state: ~1200 sources, avg crawl every 2-3 days, ~80% early-exit on content hash → ~150-250 LLM extraction calls/day + enrichment ≈ **€2-6/day LLM + €30-80/month scraping APIs + one €20-40/month server**. The bootstrap month is heavier (everything is new content).

---

## 12. Build Order

Dependency-ordered phases (each phase is independently shippable and verifiable; no calendar attached - built with coding agents, the constraint is verification, not typing):

1. **Skeleton:** schema, fetcher, JSON-LD/ICS extractor, LLM extractor, manual registry of ~40 tier-1/2 sources. Ship `/v1/occurrences` with basic filters. *Done when: real Linz events queryable via API.*
2. **Resolve & recur:** venue resolver, fingerprint dedup, rrule handling, occurrence expansion. Ingest the portals; verify cross-source merging on real collisions. *Done when: the same concert from 3 sources is one event.*
3. **Discovery + recipes:** GMaps/OSM sweep, probe-and-register, link-graph expansion, search fan-out. Recipe interpreter + onboarding agent (§5b) so new sources need zero code. Registry to 500+ sources. Adaptive scheduler. *Done when: a never-seen source goes from URL to indexed events with no human code.*
4. **Intelligence:** confidence model wiring, enrichment pass (demographics/fullness/vibe), agent search (*embeddings dropped from the critical path 2026-07-06 - hard filters + vibe-term ranking*), `.ics` feeds, `/reports`. *Done when: "something active tonight, not techno" returns sensible results.*
5. **Later (triggered, see HURDLES.md H7.2):** socials scraping, PDF/vision path, tier-D agent crawls, zero-result monitor, and a thin frontend (or just point Claude at the API and ask "what's on tonight" - which is the original itch).

The single highest-leverage early investment: **the QA loop** (nightly random re-verification + user reports adjusting source trust). Every aggregator degrades into stale garbage without it; freshness + trust is what will make this one different.

---

## 13. Business Model & Agent-Action Layer (forward-looking)

**Index, not platform** - supply is acquired unilaterally by crawling, so there is no organizer cold start. Monetization layers on top, in order of maturity:

1. **Claim-your-listing (GMB play):** organizers find their events already listed and already getting traffic → claim free (they correct data and confirm cancellations = free QA) → paid upgrades: photos, boost, booking link. The sales funnel is pre-built because supply precedes demand.
2. **Placements (Google play):** sponsored slots at query time, monetizing intent. Constraints: only viable at multi-city or agent-traffic volume, and sponsored results are always labeled and never distort organic ranking - conflict-free ranking is the core product; ads may rent space but never buy position.
3. **API/B2B:** hotels, tourism, newsletters, forecasting - standard data licensing.
4. **Agent-action layer (the endgame):** AI assistants call the API as a tool. Beyond serving events, extraction emits a `booking_schema` per event - machine-actionable registration steps, not prose:

```jsonc
"booking_schema": {
  "method": "email",                     // email | form | phone | ticket_platform | walk_in | none
  "target": "anmeldung@verein-x.at",
  "required": ["name", "persons", "date"],
  "deadline": "2026-07-09T18:00",
  "payment": {"type": "cash_at_door", "amount": 12},
  "confidence": 0.85, "as_stated": "Anmeldung bis Do 18:00 unter ..."
}
```

   The long tail books by email or simple form - trivially agent-executable and ungated (no platform can revoke email). Nobody else indexes these events, so nobody else can offer their booking. Revenue model for agent traffic: take-rate/affiliate per completed booking - monetizes the action rather than the eyeball, which is the model that survives when humans stop reading result pages. Fits the pipeline as one more extraction output + one more validation contract; agent-side execution (sending the email, filling the form) belongs to the consuming agent, not the index - the index's product is the *machine-readable instruction*, verified and confidence-scored like everything else.
