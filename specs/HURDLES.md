# Hurdles - recursive decomposition until it's boring

Method: each top-level hurdle (H1-H8) gets a solution; every solution's own residual problems become sub-hurdles (H1.1, H1.1.1, ...) and get solved in turn. Recursion stops when a leaf is marked **[leaf: boring]** = a known library, a small pure function, or a config table. If every branch bottoms out boring, the system is implementable without heroics.

The single most important de-risking decision appears first because half the tree hangs off it:

---

## H0. Canon = materialized view (the keystone decision)

**Hurdle:** Nearly every hard subsystem (dedup, merging, confidence, schema evolution, resolver bugs) is scary because mistakes seem *permanent* - a wrong merge corrupts the index forever.

**Solution:** Make the canonical layer a **pure, re-runnable function of the claims log**: `resolve(all_claims) → canon`. Claims are append-only and never touched; canon tables can be dropped and rebuilt from claims at any time. Every resolver improvement, dedup fix, or schema migration = rerun the function. This is event sourcing, and for Linz-scale data (~50k claims/year) a full rebuild is minutes, not hours.

- **H0.1 Stable event IDs across rebuilds** (consumers hold references; IDs must not churn).
  → Solution: an `identity` table mapping fingerprint-lineage → uuid, written on first sight and *never* rebuilt. The resolver assigns canon rows to existing identities by fingerprint match; only genuinely new events mint new ids. **[leaf: boring - one table + one lookup]**
- **H0.2 Rebuild while serving traffic.**
  → Solution: rebuild into `canon_next` schema, validate row counts/spot checks, swap with a transactional rename. Standard blue-green-in-Postgres. **[leaf: boring]**
- **H0.3 Claims volume growth.**
  → At ~1200 sources this is <1M rows/year. Partition by month, archive to cold storage after 18 months. **[leaf: boring]**

**Consequence:** every hurdle below that involves "what if the algorithm is wrong" downgrades from *data-corruption risk* to *rerun after fixing*. Keep this in mind - it's why the rest of the tree terminates.

---

## H1. Recurrence: messy German text → correct occurrence rows

**Hurdle:** Half the long tail is "jeden 2. Dienstag außer Ferien", gym timetables, semester plans. Getting this wrong = wrong events shown = trust dead. Raw LLM→RRULE is unreliable (models hallucinate BYDAY values, mangle exceptions).

**Solution:** Never let the LLM write RRULE. The LLM fills a tiny **constrained recurrence schema** (enums + numbers only); a deterministic compiler turns it into RRULE+EXDATE; a deterministic expander turns that into occurrences.

```jsonc
{ "freq": "weekly",            // enum: once|daily|weekly|monthly_by_weekday|irregular
  "weekday": "TU", "interval": 2, "time": "18:30",
  "except": ["school_holidays"],   // enum against a fixed exception list
  "valid_from": "2026-09-01", "valid_until": null,
  "as_stated": "jeden 2. Di außer Ferien 18:30" }  // verbatim source text, always kept
```

- **H1.1 LLM fills the schema wrong** (says TU, text says Donnerstag).
  → Solution: verification is *easier* than extraction - a second cheap-model call gets `(as_stated, compiled first 4 occurrence dates)` and answers "consistent? y/n". Disagreement → flag tentative. Checking "is 2026-09-08 a Dienstag matching this sentence" is a trivial task even for mini models. **[leaf: boring - one extra call on recurring events only]**
- **H1.2 Austrian holidays / school vacations** ("außer Ferien").
  → Static table: OÖ school holidays + national holidays, published years ahead, updated once a year. **[leaf: boring - config table]**
- **H1.3 Series identity over time** (gym publishes new timetable; is Tuesday-spinning the *same* event?).
  → Solution: series fingerprint = `(venue, weekday, time±30min, normalized_title)` - deliberately excludes dates. New timetable matches old series → update `valid_from/until`, keep id. Unmatched old series → close its validity window (don't delete). **[leaf: boring - fingerprint variant + window update]**
- **H1.4 Truly irregular** ("wenn Franz Zeit hat", "check unser Insta").
  → Don't model what isn't modelable: `freq=irregular` creates NO occurrences, event stays visible as a series with `next_occurrence=unknown`, and its source gets a slightly raised crawl frequency to catch concrete announcements. Honest unknown > fake schedule. **[leaf: boring - a status, not an algorithm]**
- **H1.5 Timetable pages with 40 classes** (one page = 40 series).
  → Treat the page as a document: snapshot-diff against last crawl's extraction; only changed rows re-verify. Bulk extraction is one LLM call with array output. **[leaf: boring]**

---

## H2. Entity resolution without ground truth

**Hurdle:** Dedup quality is unmeasurable without labels, and errors are silent (users don't report "I saw this twice", they just trust you less).

**Solution:** Three moves: (a) venue-first resolution (most event-dedup errors are venue-alias errors - solve the 300-venue problem before the 30k-event problem); (b) build a **gold set** early (during phase 2): ~150 hand-labeled claim pairs (same/different), 2-3 hours of work, becomes the permanent regression test; (c) resolver runs in shadow mode on changes - new version's merge decisions diffed against live before deploy (possible *because of H0*).

- **H2.1 Venue canonicalization itself.**
  → Venues are few (~300-500 in metro Linz) and static. GMaps place_id is the primary key for most; fuzzy alias match (trigram) against the alias list for the rest; genuinely new venue strings → tiny review queue (a few per week). **[leaf: boring - small data + human-in-loop at trickle rate]**
- **H2.2 Gold set goes stale.**
  → Every LLM adjudication in the grey zone (0.55-0.80) is logged; monthly, sample 20 into the gold set after eyeballing. Gold set grows as a by-product of operation. **[leaf: boring]**
- **H2.3 Threshold tuning (0.80 / 0.55 are guesses).**
  → They only need to be *safe*, not optimal: false-merge is the bad error, so bias thresholds high and let the grey zone (LLM adjudication) be wide. Tune against gold set precision@merge ≥ 0.98; recall can lag - a rare duplicate shown twice is embarrassing, a wrong merge is data loss. And per H0, even wrong merges are re-runnable. **[leaf: boring - one metric, one dial]**

---

## H3. The onboarding agent actually working (recipe synthesis)

**Hurdle:** "An agent explores any website and emits a working crawler config" sounds like a research project. If it only works on 50% of sites, the whole no-per-site-code promise collapses.

**Solution:** Shrink what "working" means until it's achievable, in three nested safety rings:
Ring 1: agent finds selectors + pagination → cheapest crawls.
Ring 2: agent only finds the right URLs + pagination, no selectors → LLM-extracts full page text (tier C). Slightly costlier, works on ~everything.
Ring 3: agent fails entirely → source runs as tier-D (agentic crawl) or manual.
The promise is not "the agent writes perfect scrapers", it's "no human writes scrapers" - Ring 2 alone delivers that.

- **H3.1 Agent needs browser infrastructure.**
  → Playwright + existing Python harness (user has one). One box, a pool of N contexts. Only ~20-30% of sources need JS rendering at all. **[leaf: boring - standard Playwright]**
- **H3.2 How do we know a recipe is correct at birth?**
  → Self-validation is built into onboarding (§5b): interpreter executes the fresh recipe; extracted events must overlap ≥80% with what the agent itself saw during exploration. Objective, automatic. **[leaf: boring - set comparison]**
- **H3.3 Pagination taxonomy might miss types.**
  → It's a closed set by *choice*: anything unclassifiable → Ring 2 with `pagination=none` + the agent lists concrete URLs to fetch (month URLs, category pages). URL-list-as-pagination is the universal fallback. **[leaf: boring]**
- **H3.4 Testing the interpreter without hitting live sites.**
  → Record fixtures: every onboarding stores the fetched HTML snapshots; interpreter test suite replays recipes against stored fixtures. Deterministic CI. **[leaf: boring - VCR pattern]**
- **H3.5 Agent cost/reliability at 1500-source scale.**
  → Onboarding is one-time and embarrassingly parallel; at ~€0.05-0.30/source that's <€300 for the whole registry, spread over weeks. Failures just queue for retry with a bigger model (§ model routing). **[leaf: boring - it's a batch job]**

---

## H4. Discovery precision (find 1500 real sources, not 15000 junk ones)

**Hurdle:** Search fan-out and link-graph expansion produce candidate URLs at 10:1 noise ratios. Auto-registering junk wastes crawl budget and pollutes the index; manual review of everything doesn't scale.

**Solution:** One narrow chokepoint: the **probe**. Every candidate, from any discovery channel, passes the same probe: fetch → "does this page/domain emit Linz-area events?" (cheap LLM, few-shot, returns score + evidence URL). Only score>0.8 auto-registers; 0.5-0.8 goes to a review queue; the queue is processed conversationally ("here are 14 candidates with evidence, approve/reject" - a 5-minute weekly chat task, not a UI project).

- **H4.1 Probe classifier quality unknown.**
  → Measure it the cheap way: first 100 probes get human labels (that's the review anyway), giving precision/recall numbers; adjust threshold. It's a single-prompt classifier - iterating costs minutes. **[leaf: boring]**
- **H4.2 Junk that slips through anyway.**
  → It dies economically, not by policy: a source yielding nothing gets `yield_ema→0` → crawl interval →21d → dormant after 60d (§ cost governance). Junk admission costs cents, not correctness. **[leaf: boring - already built into the scheduler]**
- **H4.3 Geographic boundary fuzziness** (is a Wels venue "Linz"?).
  → Config polygon (metro shape) + `radius` param at query time. Index generously (all of OÖ Zentralraum), filter at serve time. **[leaf: boring]**

---

## H5. Inferred attributes without ground truth (demographics, fullness)

**Hurdle:** `expected_gender_split=0.62` with no data behind it is confident nonsense - the feature that could most embarrass the product.

**Solution:** Reframe: these are **ranking features, not facts**. (a) priors live in an editable `category_priors` table (30 rows, hand-seeded, defensible: "yoga skews female, techno skews young" is common knowledge, not ML); (b) LLM only *adjusts* the prior from explicit textual evidence ("Seniorenrunde", "Frauenlauf"), never invents from nothing; (c) API labels them `estimate`, confidence capped at 0.8, UI copy says "typically"; (d) they never appear without their confidence.

> **(b) superseded 2026-07-06 (DECISIONS changelog):** Alexander relaxed it - attributes are ALWAYS estimated (LLM world knowledge allowed), confidence encodes guess-ness (~0.2 pure guess / ~0.35 typical / ≤0.8 evidenced), null only for truly inapplicable. (a), (c), (d) stand unchanged.

- **H5.1 Calibration long-term.**
  → Deferred by design: if the product grows feedback (attendance reports, user corrections via `/reports`), priors get updated from data. Until then, priors-with-humility is the correct system - there is nothing to calibrate against yet, and the architecture has the slot ready. **[leaf: boring - a table update job, later]**
- **H5.2 Fullness needs capacity data.**
  → Capacity known for the ~50 venues where fullness matters (ticketed venues publish it; GMaps/Wikipedia for the rest); everywhere else `expected_fullness=null` (= unknown, per API contract). Sold-out signals from ticket platforms override everything. **[leaf: boring - partial data is fine because null is honest]**

---

## H6. Freshness: catching cancellations in time

**Hurdle:** A cancellation posted 6 hours before the event on Facebook, while our crawl interval is 3 days. One Georg-drives-from-Steyr incident (validation S7) per month kills trust.

**Solution:** Layered, already partly in the doc, completed here:
(a) **72h proximity boost** - any source with an occurrence in the next 72h → daily crawl; day-of → morning + afternoon crawl of the *primary* source and its linked socials.
(b) **Cheap checks are nearly free**: proximity re-crawls usually end at content-hash exit; the marginal cost is a GET.
(c) **Confidence-dressed answers**: API exposes `last_confirmed_at` per occurrence; consumers can render "confirmed today 14:02" vs "last checked 3 days ago". The index's promise is calibrated, not absolute.

- **H6.1 Cancellation announced ONLY on a channel we don't crawl** (WhatsApp status, paper sign on door).
  → Unsolvable in principle; bound the damage: (1) `/reports` lets any user flag it - first reporter fixes it for everyone; (2) day-of events carry "as of <time>" phrasing downstream. Accept + be honest. **[leaf: boring - accepted residual risk, explicitly scoped]**
- **H6.2 Detecting cancellation language reliably** ("ABGESAGT", "verschoben auf...", strikethrough styling).
  → It's a classification task on *changed* pages only (hash filter already isolates them): cheap model, few-shot with German cancellation phrasings, mapped to `occurrence.status`. "Verschoben" → moved + new claim for the new date. **[leaf: boring - narrow classifier on a small stream]**

---

## H7. Operational sprawl (the meta-hurdle: one person, ten subsystems)

**Hurdle:** The doc describes discovery, scheduler, fetchers, interpreter, onboarding agent, resolver, enricher, API, QA loop, budget governor. As microservices, that's a year of plumbing. This is the hurdle that makes the whole thing "currently not straightforward".

**Solution:** **One monolith, one Postgres, one worker loop.** Every stage is a pure function `(job, db) → [new_jobs]`; the queue is a Postgres table with `SELECT ... FOR UPDATE SKIP LOCKED`; the scheduler is a cron that inserts jobs. No Kafka, no Redis, no k8s, no microservices - at 1200 sources the whole system is ~200 jobs/hour, which one process handles yawning.

```
jobs(id, kind, payload, run_after, budget_ctx, attempts, status)
kinds: crawl | onboard | probe | resolve | enrich | qa_check | discover
```

- **H7.1 Pure functions need discipline** (side effects creep).
  → Enforced by shape: every worker gets `(payload, tx)` and returns rows to insert. All state in Postgres, no in-memory anything. Testing = call function with fixture payload. **[leaf: boring - a convention + code review]**
- **H7.2 What to NOT build in v1** (deferral is a decision, not an accident).
  → v1 cuts: socials scraping (tier 4), vision/PDF path, tier-D agent crawls, ~~demographics enrichment~~ (*trigger FIRED 2026-07-06, shipped per H5 as amended*), takedown self-service (manual email suffices at zero users). v1 = portals + tier-2 institutions + recipe crawling + resolver + API. That alone beats every existing Linz portal. Each cut has a re-entry trigger: socials when ≥20 sources are Instagram-only; vision when ≥10 high-value PDF sources queue up; tier-D when ≥5 sources defeat recipes. **[leaf: boring - a scope table with triggers]**
- **H7.3 Observability without an ops stack.**
  → One `crawl_log` table + one nightly digest (rows crawled, events found, failures, spend, top degraded sources) posted to email/chat. Grafana can wait. **[leaf: boring]**

---

## H8. Instagram/Facebook (platform hostility)

**Hurdle:** A meaningful slice of the long tail announces only on Instagram. Scraping is ToS-hostile, APIs are locked down, and scraping vendors break periodically.

**Solution:** Contain it as a bounded, optional enrichment - never a load-bearing wall: (a) paid scraper API (Apify-class) rather than home-rolled evasion; (b) index only public business/organizer accounts, store extracted event facts + link, not post archives (data-minimal); (c) every Instagram-only *venue* also gets its website/GMaps registered, so losing socials degrades coverage, never correctness; (d) budget-capped like everything else.

- **H8.1 Vendor breaks.**
  → Health check = yield monitoring (already exists); on outage, sources flip to `degraded`, system keeps running without the tier. Two vendors configured, one active. **[leaf: boring - a fallback config]**
- **H8.2 Legal/ToS exposure.**
  → Public-data-only, data-minimal storage, EU-based processing, takedown honored (§9b). Risk is real but bounded and industry-standard; decide consciously, document the decision. For extra safety the tier can launch *after* the product proves itself without it (per H7.2 it's cut from v1 anyway). **[leaf: bounded decision, not engineering]**

---

## The ledger: what everything reduces to

| Scary-sounding thing | Reduces to |
|---|---|
| "Event sourcing correctness" | append-only table + pure resolve function + identity table (H0) |
| "NLP for German recurrence" | enum schema + compiler + verify-call + holiday table (H1) |
| "ML entity resolution" | venue table + trigram + weighted score + 150-pair gold set (H2) |
| "AI writes crawlers" | 3 safety rings; Ring 2 (URLs + LLM-extract) already fulfills the promise (H3) |
| "Web-scale discovery" | one probe classifier + economic junk decay (H4) |
| "Demographic prediction" | 30-row priors table + text-evidence adjustment + honest labels (H5) |
| "Real-time freshness" | 72h boost + hash-exit GETs + last_confirmed_at + user reports (H6) |
| "Distributed system" | one monolith, one Postgres, SKIP LOCKED queue, cron (H7) |
| "Social scraping arms race" | rented vendor, optional tier, cut from v1 (H8) |

Remaining genuinely-open items (small, listed for honesty): probe classifier prompt quality (hours of iteration), recurrence-schema coverage of weird German phrasings (grows with examples), and choosing the scraping vendor when the socials trigger fires. Everything else on this tree terminates in a config table, a small pure function, a standard library, or an accepted-and-documented residual risk.

Net: nothing in v1 (H7.2 scope) requires invention - only assembly. Built with coding agents, the bottleneck is not writing the code but *verifying* each phase against its done-criterion (§12 of ARCHITECTURE.md) - which is exactly what the gold set (H2), recipe self-validation (H3.2), fixture replay (H3.4), and the nightly digest (H7.3) exist to automate.
