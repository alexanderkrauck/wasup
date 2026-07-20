# Ranked MCP Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the MCP `search` tool's hand-rolled boolean-AND keyword matching (stopword list, synonym table, category mapping) with smooth ranked scoring, add an in-band hint when no results match, and stop rendering time-unknown occurrences as `00:00`.

**Architecture:** `_run_filters` in `app.py` remains the single policy/candidate gate (sex-service exclusion, suppression — §9b; do NOT duplicate it in SQL). `search` normalizes the query through Postgres's built-in German snowball stemmer (`to_tsvector('german', …)` — standard tool, replaces the hand-rolled stopword+synonym vocabulary), then ranks the policy-filtered candidate rows in Python with trigram similarity reused from `eventindex/resolve/match.py`. Fail-closed property (a query aimed at a policy-filtered event must not degrade into filler) is preserved by a mean-score threshold instead of boolean AND. `venue_address` joins the match haystack so location tokens ("Linz") resolve against addresses instead of zeroing results.

**Tech Stack:** Python 3, Postgres (built-in `german` text search config only — no new extensions, pg_trgm-style trigrams computed in Python), pytest against the real test DB (`tests/conftest.py`), FastMCP.

**Deliberate behavior changes (approved by Alexander 2026-07-15):**
- English↔German synonymy ("running" finds "Lauf") is dropped — the calling LLM translates; the hint-on-empty repairs misses.
- Multi-word natural queries degrade gracefully (ranked OR) instead of returning empty (boolean AND).
- `search`'s docstring repositions it as a name/title lookup with BAD/GOOD examples; `search_events` is the translation target.

---

### Task 1: Pure ranking helpers

**Files:**
- Modify: `src/eventindex/api/mcp_server.py` (replace lines 425–495: `_STOPWORDS`, `_SYNONYMS`, `_keyword_tokens`, `_keyword_terms`, `_keyword_categories`, `_keyword_row_matches`)
- Test: `tests/test_mcp_ranking.py` (new file — pure unit tests, no DB)

- [ ] **Step 1: Write the failing tests**

```python
"""Ranked keyword scoring for the MCP search tool: pure functions only.

Pins the two properties the boolean-AND predecessor guaranteed by
construction: fail-closed (an incidental single-word hit among noise
tokens is filler, not a result) and German compound/inflection matching.
"""

from datetime import datetime, timedelta, timezone

from eventindex.api.mcp_server import _rank_rows, _token_similarity

NOW = datetime.now(timezone.utc)


def _row(title, *, days=1.0, venue_address=None, category=None):
    return {
        "title": title,
        "starts_at": NOW + timedelta(days=days),
        "venue_name": None,
        "venue_address": venue_address,
        "organizer": None,
        "category": category or [],
    }


def test_token_similarity_tiers():
    assert _token_similarity("konzert", "konzert") == 1.0
    # German compound: stemmed token embedded in a longer word
    assert _token_similarity("konzert", "gartenkonzert") == 0.75
    # short tokens never containment-match ("run" is inside "brunnen")
    assert _token_similarity("run", "brunnen") < 0.45
    # trigram fallback survives an inflection the stemmer missed
    assert _token_similarity("posthof", "posthofs") > 0.45


def test_single_strong_token_ranks_row():
    rows = [_row("Gartenkonzert der Stadtkapelle", category=["music"])]
    assert _rank_rows(["konzert"], rows) == rows


def test_incidental_hit_among_noise_is_fail_closed():
    # the "Keramik Special" scenario: query aimed at a policy-filtered
    # event must not degrade into arbitrary single-word filler
    rows = [_row("Keramik Special", category=["culture"])]
    assert _rank_rows(["football", "loung", "night", "special"], rows) == []


def test_location_tokens_resolve_against_the_address():
    konzert = _row("Gartenkonzert", venue_address="Hauptplatz 1, 4020 Linz",
                   category=["music"])
    other = _row("Keramikmarkt", venue_address="Hauptplatz 1, 4020 Linz",
                 category=["culture"])
    ranked = _rank_rows(["konzert", "wochenend", "linz"], [other, konzert])
    assert ranked[0] is konzert  # the real match outranks the address-only one


def test_ties_break_by_start_time_and_no_tokens_is_empty():
    late = _row("Salsa Abend", days=5)
    early = _row("Salsa Abend", days=2)
    assert _rank_rows(["salsa"], [late, early]) == [early, late]
    assert _rank_rows([], [early]) == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mcp_ranking.py -v`
Expected: FAIL with `ImportError: cannot import name '_rank_rows'`

- [ ] **Step 3: Implement the helpers**

In `src/eventindex/api/mcp_server.py`, add to the imports block:

```python
from eventindex.resolve.match import _trigrams
```

(Private cross-import matches existing convention — `search` already calls `api._run_filters`.)

Delete `_STOPWORDS`, `_SYNONYMS`, `_keyword_tokens`, `_keyword_terms`, `_keyword_categories`, `_keyword_row_matches` (mcp_server.py:425–495) and put in their place:

```python
# Fail-closed thresholds (audit B1 successor): boolean AND guaranteed that a
# query aimed at a policy-filtered adult event could not degrade into
# arbitrary single-word filler. Under ranked OR the mean threshold does that
# job: one incidental exact hit among >=3 noise tokens stays below 0.35.
_MIN_BEST_SIM = 0.45   # at least one token must be a real lexical hit
_MIN_MEAN_SCORE = 0.35


def _haystack_words(row: dict) -> list[str]:
    text = " ".join(filter(None, [
        row.get("title"), row.get("venue_name"), row.get("venue_address"),
        row.get("organizer"), " ".join(row.get("category") or []),
    ]))
    return re.findall(r"[^\W\d_]+", text.lower())


def _token_similarity(token: str, word: str) -> float:
    if token == word:
        return 1.0
    if len(token) >= 4 and token in word:
        return 0.75  # German compounds/inflections: konzert | gartenkonzert
    ta, tb = _trigrams(token), _trigrams(word)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def _rank_rows(tokens: list[str], rows: list[dict]) -> list[dict]:
    """Score = mean over query tokens of the best word similarity."""
    if not tokens:
        return []
    scored = []
    for row in rows:
        words = _haystack_words(row)
        sims = [max((_token_similarity(t, w) for w in words), default=0.0)
                for t in tokens]
        score = sum(sims) / len(sims)
        if max(sims) >= _MIN_BEST_SIM and score >= _MIN_MEAN_SCORE:
            scored.append((score, row))
    scored.sort(key=lambda pair: (-pair[0], pair[1]["starts_at"]))
    return [row for _, row in scored]
```

Note: `search()` still references the deleted `_keyword_*` helpers after this step — the module won't import until Task 3 rewires it. Run only `tests/test_mcp_ranking.py` in this task; full-suite green happens at Task 3.
Wait — an import failure would break Step 4. Instead: in this task, leave the old helpers in place and only ADD the new code. Deletion of the old helpers happens in Task 3 together with the `search()` rewrite, so the module stays importable after every step.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mcp_ranking.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_ranking.py src/eventindex/api/mcp_server.py
git commit -m "Add ranked trigram scoring helpers for MCP search"
```

---

### Task 2: German query stemming via Postgres

**Files:**
- Modify: `src/eventindex/api/mcp_server.py` (next to the Task-1 helpers)
- Test: `tests/test_mcp_ranking.py` (append — uses the `conn` fixture from `tests/conftest.py`, which repoints `config.DATABASE_URL` at the test DB)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_mcp_ranking.py`:

```python
def test_stemmed_tokens_use_the_german_snowball(conn):
    from eventindex.api.mcp_server import _stemmed_tokens

    tokens = _stemmed_tokens("Konzerte am Wochenende in Linz")
    assert "konzert" in tokens      # plural stemmed
    assert "linz" in tokens
    assert "am" not in tokens       # German stopwords removed
    assert "in" not in tokens
    assert _stemmed_tokens("und für in am") == []
```

(`conn` is required only to force the test-DB rebind; the function opens its own connection.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_mcp_ranking.py::test_stemmed_tokens_use_the_german_snowball -v`
Expected: FAIL with `ImportError: cannot import name '_stemmed_tokens'`

- [ ] **Step 3: Implement**

Add below `_rank_rows` in `mcp_server.py`:

```python
def _stemmed_tokens(query: str) -> list[str]:
    """Normalize the query with Postgres's German snowball dictionary:
    stems plurals/inflections and drops German stopwords - the standard
    tool instead of a hand-rolled vocabulary. Lexeme order is irrelevant
    to the mean score in _rank_rows."""
    from eventindex import db

    with db.connect() as conn:
        row = conn.execute(
            "SELECT tsvector_to_array(to_tsvector('german', %(q)s)) AS lex",
            {"q": query[:200]},
        ).fetchone()
    return row["lex"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_mcp_ranking.py -v`
Expected: 6 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_mcp_ranking.py src/eventindex/api/mcp_server.py
git commit -m "Stem MCP search queries with Postgres's german config"
```

---

### Task 3: Rewire `search()` — ranked scoring, hint-on-empty, time-unknown label

**Files:**
- Modify: `src/eventindex/api/mcp_server.py`:
  - `StandardSearchResponse` (line ~183): add `hint` field
  - delete `_STOPWORDS`, `_SYNONYMS`, `_keyword_tokens`, `_keyword_terms`, `_keyword_categories`, `_keyword_row_matches` (the old block kept alive in Task 1)
  - rewrite `search()` (line ~498) incl. docstring
  - sharpen the `FastMCP(instructions=…)` blurb (line ~36)
- Modify: `tests/test_mcp.py`: `test_standard_search_is_hard_relevant_future_and_distinct` (line 227); check `test_chatgpt_connector_search_fetch_contract` (line 113) — its fixture event is "Chamber Concert"; if its query is a bare exact word it passes unchanged, otherwise adjust the query to an exact title word.

- [ ] **Step 1: Update the pinned integration test to the new contract**

Replace the query/assertion section of `test_standard_search_is_hard_relevant_future_and_distinct` (keep all seeding as-is):

```python
    results = _call(client, "search", {"query": "social run"})["results"]
    ids = [uuid.UUID(result["id"]) for result in results]
    # ranked OR: the double hit outranks everything, appears exactly once
    assert ids[0] == future_id
    assert ids.count(future_id) == 1
    assert ongoing_id not in ids       # past-start stays excluded
    assert polluted_id not in ids      # vibe_tags are not lexical evidence
    phrase_results = _call(client, "search", {
        "query": "football lounge nights special",
    })["results"]
    phrase_ids = {uuid.UUID(result["id"]) for result in phrase_results}
    assert exact_phrase_id in phrase_ids
    assert filler_id not in phrase_ids  # fail-closed: no single-word filler
```

(The old `all(term in title for term in ("run", "lauf", "jogging"))` assertion pinned the deleted synonym table; ranked OR legitimately also returns "Salsa Social" — a real lexical hit on "social" — below the double hit.)

- [ ] **Step 2: Run it to verify it fails against old code**

Run: `uv run pytest tests/test_mcp.py::test_standard_search_is_hard_relevant_future_and_distinct -v`
Expected: FAIL (old AND semantics return nothing for "social run" unless both words hit; assertion `ids[0] == future_id` fails on empty list)

- [ ] **Step 3: Rewrite the tool**

In `StandardSearchResponse`:

```python
class StandardSearchResponse(_Output):
    results: list[SearchResultStub]
    hint: str | None = None
```

Delete the old `_STOPWORDS`/`_SYNONYMS`/`_keyword_*` block entirely. Add the hint constant next to the thresholds:

```python
_SEARCH_HINT = (
    "No lexical match. This tool only matches words against event titles, "
    "venues, and organizers. Translate the request into structured filters "
    "and call search_events instead, e.g. filters={\"from_dt\": \"<ISO "
    "datetime>\", \"to_dt\": \"<ISO datetime>\", \"include_terms\": "
    "[\"konzert\"]}."
)
```

Replace `search()`:

```python
@mcp.tool(title="Search public Linz events by keyword", annotations=_READ_ONLY)
def search(query: str) -> StandardSearchResponse:
    """Fuzzy lexical lookup of event titles, venue names, and organizers,
    serving the standard search/fetch contract. You are the translation
    engine: convert natural-language requests into structured filters
    instead of forwarding them here.
    BAD:  search(query="Konzerte am Wochenende in Linz")
    GOOD: search_events(filters={"from_dt": ..., "to_dt": ...,
          "include_terms": ["konzert"]}) for dates, categories, prices.
    GOOD: search(query="Posthof") - name lookup is what this tool is for.
    Do not use for other cities, restaurants, private events, or
    invitations. Known commercial sex-service events and past-start
    occurrences are always excluded; fewer than ten results is preferable
    to irrelevant filler."""
    from eventindex.api import app as api

    cutoff = datetime.now(VIENNA)
    tokens = _stemmed_tokens(query)
    if not tokens:
        return StandardSearchResponse(results=[], hint=_SEARCH_HINT)
    filters = SearchFilters(**(FILTER_DEFAULTS | {"from_dt": cutoff.isoformat()}))
    payload = api._run_filters(
        filters,
        limit=2000,
        sort="starts_at",
        distinct=True,
        exclude_sex_service_context=True,
        include_inferred_terms=False,
    )
    rows = [row for row in payload["occurrences"] if row["starts_at"] >= cutoff]
    results, seen = [], set()
    for row in _rank_rows(tokens, rows):
        semantic_key = (
            re.sub(r"\W+", "", row["title"].casefold()),
            (row.get("venue_name") or "").casefold(),
        )
        if semantic_key in seen:
            continue
        seen.add(semantic_key)
        local = row["starts_at"].astimezone(VIENNA)
        when = f"{local:%a %Y-%m-%d}"
        when += " (time unknown)" if row["time_unknown"] else f" {local:%H:%M}"
        venue = f" @ {row['venue_name']}" if row.get("venue_name") else ""
        results.append(SearchResultStub(
            id=str(row["event_id"]),
            title=f"{row['title']} ({when}{venue})",
            url=_event_url(row["event_id"]),
        ))
        if len(results) >= 10:
            break
    return StandardSearchResponse(
        results=results, hint=None if results else _SEARCH_HINT)
```

In the `FastMCP(instructions=…)` blurb change

```
"search_events for structured date/category/price requests, search "
"and fetch for keyword retrieval, get_event after selecting a "
```

to

```
"search_events for structured date/category/price requests, search "
"and fetch only for name or title lookups, get_event after selecting a "
```

- [ ] **Step 4: Run the module's tests**

Run: `uv run pytest tests/test_mcp.py tests/test_mcp_ranking.py -v`
Expected: all PASS. If `test_chatgpt_connector_search_fetch_contract` or `test_submission_artifact_has_exact_stable_case_contract` fails on the new `hint` field or a synonym-era query, fix the query to an exact title word ("concert" hits "Chamber Concert" exactly) — the contract requires the `results` array shape, which is unchanged; `hint` is additive and null-free when results exist.

- [ ] **Step 5: Commit**

```bash
git add src/eventindex/api/mcp_server.py tests/test_mcp.py
git commit -m "Rank MCP search smoothly instead of boolean AND

Natural queries like 'Konzerte am Wochenende in Linz' returned zero
results because every non-matching token vetoed (live incident, ChatGPT
submission demo query). Ranked mean scoring keeps the fail-closed
filler property; the synonym/stopword vocabulary is replaced by
Postgres's german stemmer; empty results now carry a hint steering the
calling model to search_events."
```

---

### Task 4: New behavior tests (demo query, hint, time-unknown label)

**Files:**
- Test: `tests/test_mcp.py` (append)

- [ ] **Step 1: Write the tests**

```python
def test_search_handles_natural_german_queries(conn, client):
    konzert_id = _add_event(conn, "Gartenkonzert der Stadtkapelle",
                            starts=NOW + timedelta(days=2), category=["music"])
    markt_id = _add_event(conn, "Keramikmarkt am Hauptplatz",
                          starts=NOW + timedelta(days=2), category=["culture"])
    venue_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO venue (id, name, address) VALUES (%s, %s, %s)",
        (venue_id, "Donaupark", "Untere Donaulände 7, 4020 Linz"),
    )
    conn.execute("UPDATE event SET venue_id = %s WHERE id IN (%s, %s)",
                 (venue_id, konzert_id, markt_id))
    conn.commit()

    results = _call(client, "search", {
        "query": "Konzerte am Wochenende in Linz",
    })["results"]
    ids = [uuid.UUID(result["id"]) for result in results]
    assert ids[0] == konzert_id  # plural + compound + location all absorbed


def test_search_returns_hint_when_nothing_matches(client):
    out = _call(client, "search", {"query": "Quantenknödelfestival übermorgen"})
    assert out["results"] == []
    assert "search_events" in out["hint"]


def test_search_labels_unknown_times_instead_of_midnight(conn, client):
    event_id = _add_event(conn, "Sommerfest im Park",
                          starts=NOW + timedelta(days=2), category=["culture"])
    conn.execute(
        "UPDATE occurrence SET time_unknown = true WHERE event_id = %s",
        (event_id,),
    )
    conn.commit()
    results = _call(client, "search", {"query": "Sommerfest"})["results"]
    title = next(r["title"] for r in results
                 if uuid.UUID(r["id"]) == event_id)
    assert "(time unknown)" in title
    assert "00:00" not in title
```

Check the `venue` table's columns before writing the INSERT (`\d venue` or the migration file); if `address` has a different name or NOT NULL companions, adapt the INSERT — the test's point is only that the address contains "Linz".

- [ ] **Step 2: Run the new tests**

Run: `uv run pytest tests/test_mcp.py -v -k "natural_german or hint_when_nothing or unknown_times"`
Expected: 3 PASS (implementation already landed in Task 3; these pin behavior)

- [ ] **Step 3: Commit**

```bash
git add tests/test_mcp.py
git commit -m "Pin natural-query ranking, empty-result hint, time-unknown label"
```

---

### Task 5: Full verification + bookkeeping

- [ ] **Step 1: Full test suite**

Run: `uv run pytest`
Expected: all PASS (watch `test_submission_artifact_has_exact_stable_case_contract` — if it snapshots the search tool's description or output schema, regenerate/update the artifact `chatgpt-app-submission.json` accordingly and include it in the commit)

- [ ] **Step 2: DECISIONS.md changelog**

Append one line to the changelog section (match the existing line format):

```
- 2026-07-15: MCP search: boolean-AND keyword matching + hand-rolled stopword/synonym vocabulary replaced by ranked trigram scoring over the policy-filtered pool, query stemmed via Postgres 'german' config; fail-closed filler suppression now a mean-score threshold; empty results carry a search_events hint; English<->German synonymy deliberately dropped (calling LLM translates). (Alexander, chat)
```

- [ ] **Step 3: Commit**

```bash
git add DECISIONS.md
git commit -m "Record ranked-search decision"
```

Note: DECISIONS.md has pre-existing uncommitted modifications — commit only if the diff is solely the changelog line; otherwise ask Alexander how to handle the earlier edits (do not bundle them silently).

- [ ] **Step 4: Real-data spot check (if a dev DB with crawled Linz data is reachable)**

Run: `uv run python -c "from eventindex.api.mcp_server import search; print(search('Konzerte am Wochenende in Linz'))"` (or the equivalent via the test client) and paste the output in the completion note. If only the production DB has real data, note that verification happens at deploy time — and remember: netcup runs three worker units; a deploy must restart all three.
