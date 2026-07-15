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
