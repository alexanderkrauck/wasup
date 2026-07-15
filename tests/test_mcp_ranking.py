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
    # German head-final compound (+ inflection): the token ends the word
    assert _token_similarity("konzert", "gartenkonzert") == 0.75
    assert _token_similarity("konzert", "gartenkonzerte") == 0.75
    # a leading morpheme is a modifier, not the subject: it must stay
    # below the compound-head tier (a Wochentagsmesse is not a Woche)
    assert _token_similarity("wochen", "wochentagsmesse") < 0.45
    assert _token_similarity("wochen", "wochenplan") < 0.75
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


def test_dead_minority_tokens_are_dropped():
    # temporal words ("wochenend") match nothing anywhere; they must not
    # veto or dilute the real subject token
    konzert = _row("Gartenkonzert")
    other = _row("Keramikmarkt")
    assert _rank_rows(["konzert", "wochenend"], [other, konzert]) == [konzert]


def test_common_location_tokens_are_downweighted():
    # "linz" hits nearly every venue address; alone it must not pull a row
    # into the results (real-data spot check: church services topped a
    # concert query purely on address hits) - IDF makes it near-worthless
    rows = [_row(f"Angebot {i}", venue_address="4020 Linz") for i in range(24)]
    konzert = _row("Gartenkonzert", venue_address="4020 Linz")
    assert _rank_rows(["konzert", "linz"], rows + [konzert]) == [konzert]


def test_stemmed_tokens_use_the_german_snowball(conn):
    from eventindex.api.mcp_server import _stemmed_tokens

    tokens = _stemmed_tokens("Konzerte am Wochenende in Linz")
    assert "konzert" in tokens      # plural stemmed
    assert "linz" in tokens
    assert "am" not in tokens       # German stopwords removed
    assert "in" not in tokens
    assert _stemmed_tokens("und für in am") == []
