"""MCP surface for ChatGPT apps and Claude connectors, mounted at /mcp on
the one API process (stateless streamable HTTP, JSON responses - what both
directories require; HTTPS comes from Caddy).

All tools are read-only views over the same deterministic query core as
/v1/query - no LLM runs, no auth (public-data tier; the /mcp rate limit
lives in app.py). `search`/`fetch` exist verbatim because ChatGPT's chat
connector surface requires tools with exactly those names and shapes.

Directory-listing requirements covered here: every tool carries a title and
readOnlyHint/destructiveHint/openWorldHint annotations (missing ones are a
standard rejection reason), descriptions state a narrow trigger, and
responses reuse the documented public API payloads.
"""

from typing import Any
from urllib.parse import urlencode

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from eventindex.api.search import QueryBody

BASE_URL = "https://wasup.goedly.com"
_HOST = BASE_URL.removeprefix("https://")

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, openWorldHint=False
)

mcp = FastMCP(
    "Wasup - Linz Event Index",
    instructions=(
        "Public events in Linz, Austria (~25km around): the long tail no "
        "portal has, crawled from 200+ sources, deduplicated and "
        "confidence-scored. Contracts: null means unknown (never 'no'); "
        "inferred audience attributes are estimates with certainties; "
        "`projected: true` = forward-projected repetition, unconfirmed; "
        "`match_score` is ordinal, compare within one result set. "
        "When presenting results to a user: show ALL returned events (not "
        "a selection), each with its url as a clickable link, the LOCAL "
        "Europe/Vienna date+time (starts_at is UTC - always convert), "
        "venue and price; a compact table or day-grouped list beats "
        "prose. time_unknown=true means the midnight timestamp is a "
        "placeholder, never a real time. Some indexed venues are commercial "
        "sex establishments; their events carry "
        "sex_service_context=true - filter these by default (soft "
        "preference false) unless the user explicitly asks for that "
        "milieu. Full semantics: "
        f"{BASE_URL}/llms.txt"
    ),
    website_url=BASE_URL,
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    # the SDK's DNS-rebinding guard rejects unknown Host headers; Caddy
    # forwards the public host, dev/tests arrive as localhost/testserver
    transport_security=TransportSecuritySettings(
        allowed_hosts=[_HOST, "localhost:*", "127.0.0.1:*", "testserver"],
        allowed_origins=[BASE_URL, "http://localhost:*", "http://127.0.0.1:*"],
    ),
)


def _query(filters: QueryBody | None, limit: int,
           sort: str = "relevance") -> dict[str, Any]:
    from eventindex.api import app as api

    return api.query(filters or QueryBody(), limit=limit, sort=sort)


@mcp.tool(title="Search Linz events", annotations=_READ_ONLY)
def search_events(filters: QueryBody | None = None, limit: int = 20,
                  sort: str = "relevance") -> dict[str, Any]:
    """Search events in Linz, Austria by structured filters. Use for any
    'what's on in Linz' question. Window/categories/exclusions/price in
    `filters` are hard set logic (null = unknown never matches); audience
    attributes (kid_friendly, newcomer_friendly, solo_friendly, ...) are
    soft preferences ranked by your `importance` weights x the stored
    certainty - see the match_score on each row. Omit every filter the
    user did not imply - EXCEPT sex_service_context: set it false by
    default (some indexed venues are commercial sex establishments whose
    events otherwise surface in innocent queries); leave it unset only
    when the user explicitly asks for that milieu. Keep it a soft
    preference, not in required_attributes - a hard filter would also
    drop every event where the attribute is still unknown. sort="starts_at"
    gives chronological order; ask for a generous limit (default 20)
    rather than a tiny one.

    PRESENTING RESULTS: users want specifics, not a digest. Show EVERY
    returned event unless they asked for a shortlist - as a table or
    day-grouped list with: linked title (`url`), LOCAL Europe/Vienna
    date+time (starts_at is UTC!), venue, price. On time_unknown=true
    rows the midnight is a placeholder - say the time is unknown or show
    start_time_estimate as an estimate, never as fact."""
    return _query(filters, limit, sort)


@mcp.tool(title="Get event details", annotations=_READ_ONLY)
def get_event(event_id: str) -> dict[str, Any]:
    """Full record of one event by id: every field with provenance, all
    known occurrences, and the raw claims per source."""
    from uuid import UUID

    from eventindex.api import app as api

    return api.event(UUID(event_id))


@mcp.tool(title="Get calendar subscription link", annotations=_READ_ONLY)
def get_calendar_link(
    category: str | None = None,
    from_dt: str | None = None,
    to_dt: str | None = None,
    min_confidence: float | None = None,
) -> dict[str, Any]:
    """Build an .ics calendar subscription URL for a filter combination
    (category comma-separated, ISO datetimes). Give the user this URL to
    subscribe in any calendar app."""
    params = {
        k: v
        for k, v in [
            ("category", category), ("from", from_dt), ("to", to_dt),
            ("min_confidence", min_confidence),
        ]
        if v is not None
    }
    url = f"{BASE_URL}/v1/feed.ics"
    return {"ics_url": url + (f"?{urlencode(params)}" if params else "")}


# --- ChatGPT chat-connector contract: tools literally named search/fetch ---

def _event_url(row: dict) -> str:
    return row.get("url") or f"{BASE_URL}/v1/events/{row['event_id']}"


@mcp.tool(title="Search (keyword)", annotations=_READ_ONLY)
def search(query: str) -> dict[str, Any]:
    """Keyword search over upcoming Linz events; returns result stubs whose
    ids feed `fetch`. Prefer `search_events` when you can state structured
    filters."""
    from eventindex.api import app as api
    from eventindex.api.search import FILTER_DEFAULTS, SearchFilters

    filters = SearchFilters(
        **{**FILTER_DEFAULTS, "vibe_terms": query.split()}
    )
    rows = api._run_filters(filters, limit=25)["occurrences"]
    results, seen = [], set()
    for r in rows:
        if r["event_id"] in seen:
            continue
        seen.add(r["event_id"])
        venue = f" @ {r['venue_name']}" if r.get("venue_name") else ""
        results.append({
            "id": str(r["event_id"]),
            "title": f"{r['title']} ({r['starts_at']:%a %Y-%m-%d %H:%M}{venue})",
            "url": _event_url(r),
        })
        if len(results) >= 10:
            break
    return {"results": results}


@mcp.tool(title="Fetch event", annotations=_READ_ONLY)
def fetch(id: str) -> dict[str, Any]:
    """Fetch the full document for one search result id (an event id)."""
    from uuid import UUID

    from eventindex.api import app as api

    detail = api.event(UUID(id))
    e, occs = detail["event"], detail["occurrences"]
    lines = [
        f"# {e['title']}",
        f"Categories: {', '.join(e['category'] or []) or 'unknown'}",
        f"Price: {e['price_min']}-{e['price_max']}"
        if e["price_min"] is not None else "Price: unknown",
        "Dates: " + "; ".join(
            f"{o['starts_at']:%a %Y-%m-%d %H:%M}"
            + (" (projected, unconfirmed)" if o["projected"] else "")
            for o in occs
        ),
        f"Sources: {', '.join(e['provenance_summary'] or [])}",
    ]
    if e.get("inferred"):
        lines.append(f"Inferred audience attributes (estimates): {e['inferred']}")
    return {
        "id": id,
        "title": e["title"],
        "text": "\n".join(lines),
        "url": e.get("url") or f"{BASE_URL}/v1/events/{id}",
        "metadata": {"confidence": e["confidence"], "status": e["status"]},
    }
