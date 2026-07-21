# Open Questions

Protocol: the coding agent appends questions here (numbered, concrete, one-sentence-answerable). Alexander answers inline. Answered questions move to the bottom section with their answer preserved. The agent never proceeds on a blocked item by guessing - it switches to unblocked work.

## Open

9. **Embeddings provider** - *premise updated 2026-07-08: phase 4 shipped WITHOUT embeddings (agent search = hard filters + vibe-term-overlap ranking; title matching = trigram + word containment). Nothing is blocked on this anymore.* If real usage shows ranking lacking: OK to add a direct OpenAI/Voyage key just for embeddings (~cents/month), or defer indefinitely?
11. **Venue review** - weekly 5-min skim of `var/review/venues-*.md` and `suppressed-*.md` dumps for obvious junk (latest rebuilds append; the 2026-07-05 file with 132 venues is still unreviewed).
16. **event.lang column** - null on all 6,104 events (red team 2026-07-21): fill it (one field in the extraction schema, ~free) or drop the column? One word: fill / drop.
17. **Enrichment-based field completion** - 4 of 6 audited price gaps were stated on the page (once even inside our own stored description). Extending the enrichment schema with stated_price/venue extraction re-enriches the corpus (~EUR 1-2, SCHEMA_VERSION bump). OK to spend once the OpenRouter balance is topped up? y/n.

## Answered

15. **Locality gate for aggregator junk** → applied, **decided by the agent under the 2026-07-10 full-autonomy grant - veto here if wrong.** Evidence: Boston/Las Vegas career fairs and a NASA launch were being served as Linz events (all Eventbrite, non-.at URLs, zero locality data). Gate (rebuild-side, pure function, fully reversible): only-global-platform provenance (eventbrite|meetup) AND no venue AND no geo AND non-.at event URL → unpublished; suppressed titles dumped to var/review/aggregator-junk-*.md. Austria-local aggregators (linztermine/tips/meinbezirk/eventfinder) are exempt by design - their placeless events are real events with lazy markup. *(2026-07-10)*

13. **Ticketmaster Discovery API key** → **no** (Alexander, 2026-07-09): Austria is Eventim country, Linz inventory thin; prices/images come from booking-schema extraction (backlog #8) instead. *(2026-07-09)*
14. **Bandsintown partner app_id** → **no** ("fuck em", Alexander, 2026-07-09): artist-gig long tail for Linz is tiny, music already the strongest category. Phase A closed: tips.at + meinbezirk aggregators onboarded and crawling; Eventbrite/Meetup were already crawled; ra.co/Bandsintown/Songkick bot-walled (no evasion arms race), Eventbrite API dead for public search since 2020, Songkick API closed. *(2026-07-09)*

7. **Domain name** -> **wasup.at**, product name "Wasup" (Alexander, 2026-07-09). Registration + A records his side; TLS/key-rotation/branding pass on DNS. *(2026-07-09)*

12. **Search API for §4d fan-out** → resolved with NO new account: OpenRouter web plugin (Exa engine), URLs via url_citation annotations, ~€1/month for 160 queries, budget-ledgered through the one LLM client. Research trail: Google CSE closed to new customers & dead 2027-01 (Alexander's screenshot confirmed); Gemini grounding free but ToS forbids using links for crawling; Brave vetoed by Alexander. Smoke test: "run club linz" → howwasyourdayclub.com at rank 2. *(2026-07-06)*

1. **LLM provider/key** → OpenRouter. Key itself still pending (→ #8). *(2026-07-03)*
2. **Runtime target for phases 0-2** → local, Postgres 16 + PostGIS + pgvector as a single Docker container (dev DB only; deploy target stays VPS+systemd per DECISIONS.md). *(2026-07-03)*
3. **Crawler contact identity** → alexander.krauck@gmail.com for now; may switch to a dedicated address later. *(2026-07-03)* → switched to alexander@business.goedly.com everywhere public (pages, UA, README, pyproject). *(2026-07-15)*
6. **Repo home** → git init in the local folder; GitHub remote deferred. *(2026-07-03)*
8. **OpenRouter API key** → provided in `.env`; verified with a live structured-output call (dummy crawl, €0.0004 recorded in ledger). *(2026-07-03)*
10. **Gold-set labeling** → Alexander delegated to a labeling agent (his call, 2026-07-05): all 123 pairs labeled with DB/web research, zero undecidable. Deviation from H2 "hand-labeled" signed off in chat; borderline product-calls (hall-naming = same, separate showtimes = different) locked as label policy. precision@merge = 16/16 = 1.0 ≥ 0.98 → **Phase 2 criterion (d) met**. *(2026-07-05)*
4. **Google Places API** → key provided in `.env` as `GOOGLE_PLACES_API_KEY`. *(2026-07-06)*
5. **Agent harness** → assessed god-in-a-box (Alexander: DASC/"SN" experimental = excluded; "variation of codact" = tool-calling loop). Verdict: don't embed - no sandbox/turn-cap/cost-cap on the codeact path, heavy deps, invasive side effects, untested loop; instead a purpose-built ~400-line browser-tool loop in eventindex, harvesting its graceful-timeout + trajectory patterns. **Decided provisionally by the agent while Alexander was AFK - veto here if wrong.** *(2026-07-06)*
