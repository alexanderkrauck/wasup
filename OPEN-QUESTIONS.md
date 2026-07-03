# Open Questions

Protocol: the coding agent appends questions here (numbered, concrete, one-sentence-answerable). Alexander answers inline. Answered questions move to the bottom section with their answer preserved. The agent never proceeds on a blocked item by guessing - it switches to unblocked work.

## Open

4. **Google Places API** - OK to create a key with billing (discovery sweep, ~€0-50 one-time within free credit)? Alternative: OSM-only start (free, ~80% of venue coverage).
5. **Agent harness** - you mentioned you have a Python agent harness; repo/path so the onboarding agent (phase 3) builds on it instead of a new one?
7. **Domain name** - needed by phase 4 at the latest (.ics URLs, API keys); any preference, or defer?
8. **OpenRouter API key** - provider chosen (see #1); put the key in `.env` as `OPENROUTER_API_KEY` when ready - blocks the Phase 0 done-criterion demo, nothing before it.

## Answered

1. **LLM provider/key** → OpenRouter. Key itself still pending (→ #8). *(2026-07-03)*
2. **Runtime target for phases 0-2** → local, Postgres 16 + PostGIS + pgvector as a single Docker container (dev DB only; deploy target stays VPS+systemd per DECISIONS.md). *(2026-07-03)*
3. **Crawler contact identity** → alexander.krauck@gmail.com for now; may switch to a dedicated address later. *(2026-07-03)*
6. **Repo home** → git init in the local folder; GitHub remote deferred. *(2026-07-03)*
