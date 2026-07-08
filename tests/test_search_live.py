"""50-query agent-search gate (Phase 4 done-criterion a): exclusions and hard
filters must NEVER leak into results, across realistic German/English queries.

Marked `live`: hits the LIVE DB and spends real LLM budget (~50 mini parses,
cents). Deselected by default; run it ALONE so the conftest test-db switch
never activates:

    uv run pytest -m live tests/test_search_live.py
"""

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.live

# (query, expects_exclusion): the second field asserts the parser recognized
# an explicit negation at all - the leak check below then guarantees it held.
QUERIES = [
    ("was geht heute abend, nicht techno, unter 20€", True),
    ("something active tonight, not techno, under 20 euro", True),
    ("morgen abend tanzen gehen, kein ballett", True),
    ("konzert am wochenende, aber nicht klassik", True),
    ("events heute ohne sport", True),
    ("kino diese woche, kein horror", True),
    ("was kann ich morgen mit kindern machen, nichts religiöses", True),
    ("party am samstag, kein schlager", True),
    ("etwas ruhiges heute abend, kein konzert", True),
    ("free events this weekend, no markets", True),
    ("workshop nächste woche, nicht online, kein tech", True),
    ("ausgehen heute, aber kein theater und keine oper", True),
    ("flohmarkt am sonntag, nicht in urfahr", True),
    ("live musik freitag, kein jazz", True),
    ("was geht ab, ohne fußball", True),
    ("veranstaltungen für senioren, keine kirche", True),
    ("brunch oder food event sonntag, kein festival", True),
    ("lesung oder vortrag, nicht auf englisch", True),
    ("chillen im park, keine familienevents", True),
    ("club heute nacht, kein hip hop", True),
    ("was geht heute abend in linz", False),
    ("konzerte am wochenende", False),
    ("events morgen abend", False),
    ("what's on tonight", False),
    ("gratis veranstaltungen diese woche", False),
    ("tanzen gehen am freitag", False),
    ("yoga kurse nächste woche", False),
    ("etwas mit hoher energie heute, leute 20-30", False),
    ("chill activity today with people 20-30, at least half women", False),
    ("wo kann ich neue leute kennenlernen diese woche", False),
    ("familienausflug am sonntag", False),
    ("theater im juli", False),
    ("open air kino im sommer", False),
    ("markt am samstag vormittag", False),
    ("sportevents zum mitmachen", False),
    ("kunstausstellung diese woche", False),
    ("pub quiz oder spieleabend", False),
    ("klassik konzert unter 30 euro", False),
    ("events für studenten heute", False),
    ("was geht am donnerstag abend", False),
    ("techno party am wochenende", False),
    ("kabarett oder comedy im juli", False),
    ("something outdoors tomorrow", False),
    ("weinverkostung oder food events", False),
    ("kostenlose konzerte im park", False),
    ("events in urfahr heute", False),
    ("salsa oder tango tanzen", False),
    ("vortrag über technik oder wissenschaft", False),
    ("kindertheater am nachmittag", False),
    ("was ist morgen früh los", False),
]


def _dt(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


@pytest.mark.parametrize("query,expects_exclusion", QUERIES)
def test_hard_filters_never_leak(query, expects_exclusion):
    from eventindex.api.app import app

    resp = TestClient(app).get("/v1/search", params={"q": query})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    f = body["parsed_filters"]

    if expects_exclusion:
        assert f["exclude_categories"] or f["exclude_terms"], (
            f"parser missed the negation in: {query!r} -> {f}"
        )

    for occ in body["occurrences"]:
        cats = set(occ["category"] or [])
        leaked = cats & set(f["exclude_categories"])
        assert not leaked, f"{query!r}: {occ['title']!r} leaked {leaked}"
        title = (occ["title"] or "").lower()
        for term in f["exclude_terms"]:
            assert term.lower() not in title, (
                f"{query!r}: excluded term {term!r} in {occ['title']!r}"
            )
        if f["max_price"] is not None and not f["is_free"] \
                and occ["price_min"] is not None:
            assert float(occ["price_min"]) <= f["max_price"], (
                f"{query!r}: {occ['title']!r} over price cap"
            )
        # parsed_filters are validator-normalized to tz-aware ISO strings;
        # a naive one slipping through here IS the bug, so no guard clause
        starts = _dt(occ["starts_at"])
        window_from, window_to = _dt(f["from_dt"]), _dt(f["to_dt"])
        if starts and window_from:
            assert starts >= window_from, f"{query!r}: {occ['title']!r} before window"
        if starts and window_to:
            assert starts <= window_to, f"{query!r}: {occ['title']!r} after window"
