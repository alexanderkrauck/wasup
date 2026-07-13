"""Agent-search semantics: exclusions are guarantees, hard filters are set
logic, soft attribute preferences combine importance x certainty and never
drop rows. Parser itself is stubbed (LLM); build_sql/scoring are the
testable deterministic core."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from eventindex.api.search import (
    ATTRIBUTES, FILTER_DEFAULTS, SearchFilters, UNKNOWN_PRIOR, attribute_select,
    build_sql, preference_score, rank,
)

NOW = datetime.now(timezone.utc)


def _filters(**kw):
    return SearchFilters(**(FILTER_DEFAULTS | kw))


def _add(conn, title, *, category=None, age=None, energy=None, vibe=None,
         kid=None, kid_conf=0.6, price=None, gender=None, gender_conf=None,
         venue=None):
    event_id = uuid.uuid4()
    venue_id = None
    if venue:
        venue_id = uuid.uuid4()
        conn.execute("INSERT INTO venue (id, name) VALUES (%s, %s)",
                     (venue_id, venue))
    inferred = {}
    if energy:
        inferred["energy"] = energy
    if vibe:
        inferred["vibe_tags"] = vibe
    if kid is not None:
        inferred["kid_friendly"] = {"value": kid, "confidence": kid_conf}
    from psycopg.types.json import Jsonb
    conn.execute(
        "INSERT INTO event (id, kind, title, category, confidence, status, "
        "expected_age_range, expected_age_range_confidence, "
        "expected_gender_split, expected_gender_split_confidence, "
        "inferred, price_min, venue_id) VALUES "
        "(%s, 'one_off', %s, %s, 0.9, 'confirmed', "
        " CASE WHEN %s::int IS NULL THEN NULL ELSE int4range(%s, %s, '[]') END, "
        " 0.5, %s, %s, %s, %s, %s)",
        (event_id, title, category or [],
         age[0] if age else None, age[0] if age else None,
         age[1] if age else None, gender, gender_conf,
         Jsonb(inferred) if inferred else None, price, venue_id),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s)",
        (event_id, NOW + timedelta(days=1)),
    )


def _run(conn, filters):
    """Hard SQL + soft ranking, like the API core."""
    where, params = build_sql(filters)
    params["limit"] = 50
    rows = conn.execute(
        f"SELECT e.title, 0.9::float AS confidence, e.category, "
        f"e.inferred->'vibe_tags' AS vibe_tags, {attribute_select()} "
        f"FROM occurrence o JOIN event e ON e.id = o.event_id "
        f"LEFT JOIN venue v ON v.id = e.venue_id "
        f"WHERE {where} LIMIT %(limit)s", params,
    ).fetchall()
    return [r["title"] for r in rank(rows, filters)]


# ------------------------------------------------------------- guarantees

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


def test_include_terms_require_one_synonym_word_boundary_aware(conn):
    _add(conn, "HWYD Social Run")
    _add(conn, "FUN-Orientierungslauf Solar City")   # compound suffix: match
    _add(conn, "Führung: Max Pechstein")             # 'run' inside: NO match
    _add(conn, "Salsa Night", vibe=["running"])      # tag match
    conn.commit()
    titles = _run(conn, _filters(include_terms=["lauf", "run", "running"]))
    assert "Führung: Max Pechstein" not in titles
    assert set(titles) == {"HWYD Social Run", "FUN-Orientierungslauf Solar City",
                           "Salsa Night"}


def test_window_strings_validated_and_vienna_pinned():
    f = _filters(from_dt="2026-07-08T17:00", to_dt="2026-07-08T23:59")
    assert f.from_dt.endswith("+02:00")  # naive LLM output -> Europe/Vienna
    _, params = build_sql(f)
    assert params["from"].tzinfo is not None  # SQL sees a datetime, not text
    with pytest.raises(ValidationError):
        _filters(from_dt="morgen abend")  # non-ISO never reaches the DB


def test_free_filter(conn):
    _add(conn, "Gratis Konzert", price=0)
    _add(conn, "Teures Konzert", price=40)
    conn.commit()
    assert _run(conn, _filters(is_free=True)) == ["Gratis Konzert"]


# ------------------------------------------- required = hard, null excluded

def test_required_attribute_never_matches_unknown(conn):
    _add(conn, "Studentenparty", age=(20, 30))
    _add(conn, "Unknown Audience Thing")
    conn.commit()
    titles = _run(conn, _filters(age_min=20, age_max=30,
                                 required_attributes=["age"]))
    assert titles == ["Studentenparty"]  # null = unknown, hard-excluded


def test_required_kid_friendly_is_set_logic(conn):
    _add(conn, "Kinderfest", kid=True)
    _add(conn, "HIIT Bootcamp", kid=False)
    _add(conn, "Mystery Meetup")  # unknown
    conn.commit()
    titles = _run(conn, _filters(kid_friendly=True,
                                 required_attributes=["kid_friendly"]))
    assert titles == ["Kinderfest"]


# --------------------------------------- soft: importance x certainty ranks

def test_soft_preference_ranks_but_never_drops(conn):
    _add(conn, "Kinderfest", kid=True, kid_conf=0.8)
    _add(conn, "HIIT Bootcamp", kid=False, kid_conf=0.8)
    _add(conn, "Mystery Meetup")  # unknown
    conn.commit()
    titles = _run(conn, _filters(kid_friendly=True))
    assert len(titles) == 3  # nothing dropped
    # confident match > unknown (prior) > confident contradiction
    assert titles == ["Kinderfest", "Mystery Meetup", "HIIT Bootcamp"]


def test_certainty_orders_the_spectrum():
    f = _filters(kid_friendly=True)

    def p(value, conf):
        return preference_score(
            {"kid_friendly__value": value, "kid_friendly__conf": conf}, f
        )

    # strong match > weak match > unknown > weak contra > strong contra
    assert p(True, 0.8) > p(True, 0.2) > p(None, None) > p(False, 0.2) > p(False, 0.8)
    assert p(None, None) == UNKNOWN_PRIOR


def test_importance_weights_shift_the_ranking():
    f = _filters(kid_friendly=True, gender_split_min=0.5)
    kid_yes_gender_no = {
        "kid_friendly__value": True, "kid_friendly__conf": 0.8,
        "gender_split_min__value": 0.3, "gender_split_min__conf": 0.8,
    }
    kid_no_gender_yes = {
        "kid_friendly__value": False, "kid_friendly__conf": 0.8,
        "gender_split_min__value": 0.7, "gender_split_min__conf": 0.8,
    }
    gender_matters = {"gender_split_min": 1.0, "kid_friendly": 0.1}
    kids_matter = {"gender_split_min": 0.1, "kid_friendly": 1.0}
    assert preference_score(kid_no_gender_yes, f, gender_matters) > \
        preference_score(kid_yes_gender_no, f, gender_matters)
    assert preference_score(kid_yes_gender_no, f, kids_matter) > \
        preference_score(kid_no_gender_yes, f, kids_matter)


def test_age_overlap_soft_scoring():
    f = _filters(age_min=20, age_max=30)
    inside = {"age__lo": 18, "age__hi": 34, "age__conf": 0.6}  # [18,33]
    outside = {"age__lo": 50, "age__hi": 71, "age__conf": 0.6}
    unknown = {"age__lo": None, "age__hi": None, "age__conf": None}
    assert preference_score(inside, f) == 0.8    # 0.5 + 0.6/2
    assert abs(preference_score(outside, f) - 0.2) < 1e-9
    assert preference_score(unknown, f) == UNKNOWN_PRIOR


def test_vibe_terms_match_compounds_not_substrings():
    f = _filters(vibe_terms=["run", "lauf"])
    rows = [
        {"title": "Führung: Max Pechstein", "vibe_tags": [], "category": [],
         "confidence": 0.9},
        {"title": "FUN-Orientierungslauf Solar City", "vibe_tags": [],
         "category": [], "confidence": 0.9},
        {"title": "HWYD Social Run", "vibe_tags": [], "category": [],
         "confidence": 0.9},
    ]
    ranked = rank(rows, f)
    # "run" inside "Führung" is not a hit; compound suffix "lauf" is
    assert {r["title"] for r in ranked[:2]} == {
        "HWYD Social Run", "FUN-Orientierungslauf Solar City",
    }
    assert ranked[-1]["title"].startswith("Führung")


def test_vibe_terms_rank_but_never_exclude():
    f = _filters(vibe_terms=["dance"])
    rows = [
        {"title": "Salsa Night", "vibe_tags": ["dance", "energetic"],
         "category": ["nightlife"], "confidence": 0.8},
        {"title": "Chess Evening", "vibe_tags": ["quiet"],
         "category": ["community"], "confidence": 0.9},
    ]
    ranked = rank(rows, f)
    assert ranked[0]["title"] == "Salsa Night"  # ranked up...
    assert len(ranked) == 2                     # ...but nothing dropped
    assert all("match_score" in r for r in ranked)


# ------------------------------------------------------------- registry

def test_filter_defaults_cover_the_whole_model():
    assert set(FILTER_DEFAULTS) == set(SearchFilters.model_fields)


def test_registry_covers_every_soft_filter_field():
    from eventindex.api.search import SOFT_ATTRIBUTES

    soft = {"gender_split_min", "kid_friendly", "newcomer_friendly",
            "outdoor", "energy", "language", "age", "solo_friendly",
            "interaction_structure", "sex_service_context"}
    assert soft == set(ATTRIBUTES) == SOFT_ATTRIBUTES


def test_unknown_required_attribute_is_rejected():
    with pytest.raises(ValidationError):
        _filters(required_attributes=["favourite_color"])


def test_include_terms_match_venue_name(conn):
    """'events from factory300' names a venue/organizer, not a title word -
    a consumer query came back empty over this (2026-07-12)."""
    _add(conn, "Community Oktoberfest", venue="factory300")
    _add(conn, "Sommerkonzert")
    titles = _run(conn, _filters(include_terms=["factory300"]))
    assert titles == ["Community Oktoberfest"]


def test_exclude_terms_match_venue_and_spare_venueless(conn):
    _add(conn, "Community Oktoberfest", venue="factory300")
    _add(conn, "Sommerkonzert")  # no venue: must NOT be null-poisoned out
    titles = _run(conn, _filters(exclude_terms=["factory300"]))
    assert titles == ["Sommerkonzert"]


def test_multiword_terms_match_hyphen_and_compound(conn):
    """B4: 'krone fest' found neither 'Krone-Fest' nor 'Kronefest'."""
    _add(conn, "Linzer Krone-Fest 2026")
    _add(conn, "SoulSanity LIVE @ Linzer Kronefest")
    _add(conn, "Kronleuchter-Ausstellung")
    titles = _run(conn, _filters(include_terms=["krone fest"]))
    assert set(titles) == {"Linzer Krone-Fest 2026",
                           "SoulSanity LIVE @ Linzer Kronefest"}


def test_include_terms_match_organizer(conn):
    _add(conn, "Sommerfest")  # no organizer
    event_id = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, organizer, confidence, status) "
        "VALUES (%s, 'one_off', 'Netzwerkabend', 'tech2b', 0.9, 'confirmed')",
        (event_id,),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s)",
        (event_id, NOW + timedelta(days=1)),
    )
    titles = _run(conn, _filters(include_terms=["tech2b"]))
    assert titles == ["Netzwerkabend"]
