"""Agent-search semantics: exclusions are guarantees, filters are set logic,
vibe terms only rank. Parser itself is stubbed (LLM); build_sql/rank are the
testable deterministic core."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from eventindex.api.search import SearchFilters, build_sql, rank

NOW = datetime.now(timezone.utc)


def _filters(**kw):
    base = dict(
        from_dt=None, to_dt=None, categories=None, exclude_categories=[],
        exclude_terms=[], age_min=None, age_max=None, gender_split_min=None,
        max_price=None,
        is_free=None, kid_friendly=None, newcomer_friendly=None, outdoor=None,
        energy=None, language=None, vibe_terms=[],
    )
    base.update(kw)
    return SearchFilters(**base)


def _add(conn, title, *, category=None, age=None, energy=None, vibe=None,
         kid=None, price=None):
    event_id = uuid.uuid4()
    inferred = {}
    if energy:
        inferred["energy"] = energy
    if vibe:
        inferred["vibe_tags"] = vibe
    if kid is not None:
        inferred["kid_friendly"] = {"value": kid, "confidence": 0.6}
    from psycopg.types.json import Jsonb
    conn.execute(
        "INSERT INTO event (id, kind, title, category, confidence, status, "
        "expected_age_range, inferred, price_min) VALUES "
        "(%s, 'one_off', %s, %s, 0.9, 'confirmed', "
        " CASE WHEN %s::int IS NULL THEN NULL ELSE int4range(%s, %s, '[]') END, %s, %s)",
        (event_id, title, category or [],
         age[0] if age else None, age[0] if age else None,
         age[1] if age else None, Jsonb(inferred) if inferred else None, price),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s)",
        (event_id, NOW + timedelta(days=1)),
    )


def _run(conn, filters):
    where, params = build_sql(filters)
    params["limit"] = 50
    return [r["title"] for r in conn.execute(
        f"SELECT e.title FROM occurrence o JOIN event e ON e.id = o.event_id "
        f"WHERE {where} LIMIT %(limit)s", params,
    )]


def test_exclusions_are_leakproof(conn):
    _add(conn, "Techno Rave", category=["nightlife"], vibe=["techno"])
    _add(conn, "Jazz Brunch", category=["music"], vibe=["jazz", "calm"])
    conn.commit()
    titles = _run(conn, _filters(exclude_categories=["nightlife"]))
    assert "Techno Rave" not in titles
    titles = _run(conn, _filters(exclude_terms=["techno"]))
    assert "Techno Rave" not in titles
    assert "Jazz Brunch" in titles


def test_exclude_terms_do_not_drop_unenriched_events(conn):
    # inferred IS NULL must mean "judge by title", never "hide the event"
    _add(conn, "Sommerfest im Park")  # no inferred at all
    _add(conn, "Techno Nacht")
    conn.commit()
    titles = _run(conn, _filters(exclude_terms=["techno"]))
    assert "Sommerfest im Park" in titles
    assert "Techno Nacht" not in titles


def test_window_strings_validated_and_vienna_pinned():
    f = _filters(from_dt="2026-07-08T17:00", to_dt="2026-07-08T23:59")
    assert f.from_dt.endswith("+02:00")  # naive LLM output -> Europe/Vienna
    _, params = build_sql(f)
    assert params["from"].tzinfo is not None  # SQL sees a datetime, not text
    with pytest.raises(ValidationError):
        _filters(from_dt="morgen abend")  # non-ISO never reaches the DB


def test_age_filter_never_matches_unknown(conn):
    _add(conn, "Studentenparty", age=(20, 30))
    _add(conn, "Unknown Audience Thing")
    conn.commit()
    titles = _run(conn, _filters(age_min=20, age_max=30))
    assert titles == ["Studentenparty"]  # null = unknown, excluded


def test_energy_and_kid_filters(conn):
    _add(conn, "HIIT Bootcamp", energy="high", kid=False)
    _add(conn, "Kinderfest", energy="medium", kid=True)
    conn.commit()
    assert _run(conn, _filters(energy="high")) == ["HIIT Bootcamp"]
    assert _run(conn, _filters(kid_friendly=True)) == ["Kinderfest"]


def test_vibe_terms_rank_but_never_exclude(conn):
    rows = [
        {"title": "Salsa Night", "vibe_tags": ["dance", "energetic"],
         "category": ["nightlife"], "confidence": 0.8},
        {"title": "Chess Evening", "vibe_tags": ["quiet"],
         "category": ["community"], "confidence": 0.9},
    ]
    ranked = rank(rows, ["dance"])
    assert ranked[0]["title"] == "Salsa Night"  # ranked up...
    assert len(ranked) == 2                     # ...but nothing dropped


def test_free_filter(conn):
    _add(conn, "Gratis Konzert", price=0)
    _add(conn, "Teures Konzert", price=40)
    conn.commit()
    assert _run(conn, _filters(is_free=True)) == ["Gratis Konzert"]
