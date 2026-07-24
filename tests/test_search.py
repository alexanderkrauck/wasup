"""Agent-search semantics: exclusions are guarantees, hard filters are set
logic, soft attribute preferences combine importance x certainty and never
drop rows. Parser itself is stubbed (LLM); build_sql/scoring are the
testable deterministic core."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from eventindex.api.search import (
    ATTRIBUTES, FILTER_DEFAULTS, REQUIRED_ATTRIBUTES, SOFT_ATTRIBUTES,
    SearchFilters, UNKNOWN_PRIOR, attribute_select, build_sql,
    preference_score, rank,
)

NOW = datetime.now(timezone.utc)


def _filters(**kw):
    return SearchFilters(**(FILTER_DEFAULTS | kw))


def _add(conn, title, *, category=None, age=None, energy=None, tags=None,
         kid=None, kid_conf=0.6, price=None, gender=None, gender_conf=None,
         venue=None, organizer=None, attendance=None, attendance_conf=None,
         estimated_price=None):
    event_id = uuid.uuid4()
    venue_id = None
    if venue:
        venue_id = uuid.uuid4()
        conn.execute("INSERT INTO venue (id, name) VALUES (%s, %s)",
                     (venue_id, venue))
    inferred = {}
    if energy:
        inferred["energy"] = energy
    if kid is not None:
        inferred["kid_friendly"] = {"value": kid, "confidence": kid_conf}
    if estimated_price is not None:
        inferred["price"] = {
            "min": estimated_price, "max": estimated_price,
            "currency": "EUR", "basis": "estimated", "confidence": 0.2,
        }
    from psycopg.types.json import Jsonb
    conn.execute(
        "INSERT INTO event (id, kind, title, category, confidence, status, "
        "expected_age_range, expected_age_range_confidence, "
        "expected_gender_split, expected_gender_split_confidence, "
        "expected_attendance, expected_attendance_confidence, "
        "inferred, price_min, venue_id, organizer) VALUES "
        "(%s, 'one_off', %s, %s, 0.9, 'confirmed', "
        " CASE WHEN %s::int IS NULL THEN NULL ELSE int4range(%s, %s, '[]') END, "
        " 0.5, %s, %s, %s, %s, %s, %s, %s, %s)",
        (event_id, title, category or [],
         age[0] if age else None, age[0] if age else None,
         age[1] if age else None, gender, gender_conf,
         attendance, attendance_conf,
         Jsonb(inferred) if inferred else None, price, venue_id, organizer),
    )
    conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s)",
        (event_id, NOW + timedelta(days=1)),
    )
    for tag in tags or []:
        conn.execute(
            "INSERT INTO event_tag (event_id, name, confidence, origins) "
            "VALUES (%s, %s, 0.8, '{inferred}')",
            (event_id, tag),
        )
    return event_id


def _run(conn, filters):
    """Hard SQL + soft ranking, like the API core."""
    where, params = build_sql(filters)
    params["limit"] = 50
    rows = conn.execute(
        f"SELECT e.id AS event_id, e.title, 0.9::float AS confidence, e.category, "
        f"{attribute_select()} "
        f"FROM occurrence o JOIN event e ON e.id = o.event_id "
        f"LEFT JOIN venue v ON v.id = e.venue_id "
        f"WHERE {where} LIMIT %(limit)s", params,
    ).fetchall()
    exact_scores = {
        row["event_id"]: 0.8
        for row in rows
        if conn.execute(
            "SELECT 1 FROM event_tag WHERE event_id = %s AND name = ANY(%s)",
            (row["event_id"], filters.tags),
        ).fetchone()
    } if filters.tags else {}
    return [r["title"] for r in rank(rows, filters, tag_scores=exact_scores)]


# ------------------------------------------------------------- guarantees

def test_exclusions_are_leakproof(conn):
    _add(conn, "Techno Rave", category=["nightlife"], tags=["techno"])
    _add(conn, "Jazz Brunch", category=["music"], tags=["jazz", "calm"])
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


def test_name_is_title_scoped_and_compound_suffix_aware(conn):
    _add(conn, "Maturaball der HLW")
    _add(conn, "Fußball Turnier")
    _add(conn, "Ballett Premiere")
    _add(conn, "Salsa Night", tags=["ball"])
    conn.commit()
    titles = _run(conn, _filters(name="ball"))
    assert set(titles) == {"Maturaball der HLW", "Fußball Turnier"}


def test_window_strings_validated_and_vienna_pinned():
    f = _filters(from_dt="2026-07-08T17:00", to_dt="2026-07-08T23:59")
    assert f.from_dt.endswith("+02:00")  # naive LLM output -> Europe/Vienna
    _, params = build_sql(f)
    assert params["from"].tzinfo is not None  # SQL sees a datetime, not text
    with pytest.raises(ValidationError):
        _filters(from_dt="morgen abend")  # non-ISO never reaches the DB


def test_weekdays_are_a_local_hard_occurrence_filter():
    f = _filters(weekdays=["thursday", "friday"])
    where, params = build_sql(f)
    assert "AT TIME ZONE 'Europe/Vienna'" in where
    assert params["weekdays"] == [4, 5]
    with pytest.raises(ValidationError):
        _filters(weekdays=["freitag"])


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


def test_tags_rank_but_never_exclude_or_exceed_one():
    f = _filters(tags=["dance"])
    salsa_id, chess_id = uuid.uuid4(), uuid.uuid4()
    rows = [
        {"event_id": salsa_id, "title": "Salsa Night",
         "category": ["nightlife"], "confidence": 0.8},
        {"event_id": chess_id, "title": "Chess Evening",
         "category": ["community"], "confidence": 0.9},
    ]
    ranked = rank(rows, f, tag_scores={salsa_id: 0.75, chess_id: 0.05})
    assert ranked[0]["title"] == "Salsa Night"  # ranked up...
    assert len(ranked) == 2                     # ...but nothing dropped
    assert all(0 <= r["match_score"] <= 1 for r in ranked)


def test_stronger_query_fit_beats_higher_whole_event_confidence():
    f = _filters(tags=["dance"])
    strong_id, weak_id = uuid.uuid4(), uuid.uuid4()
    rows = [
        {"event_id": strong_id, "title": "Dance Ball", "confidence": 0.4},
        {"event_id": weak_id, "title": "Ball Sport", "confidence": 0.95},
    ]
    ranked = rank(rows, f, tag_scores={strong_id: 0.8, weak_id: 0.4})
    assert [row["title"] for row in ranked] == ["Dance Ball", "Ball Sport"]
    assert ranked[0]["match_score"] == 0.8


def test_tag_intent_leads_secondary_preferences_unless_importance_overrides():
    f = _filters(tags=["dance"], preferred_max_price=20)
    dance_id, free_talk_id = uuid.uuid4(), uuid.uuid4()
    rows = [
        {
            "event_id": dance_id, "title": "Dance Night", "confidence": 0.8,
            "price__value": 35, "price__conf": 0.8,
        },
        {
            "event_id": free_talk_id, "title": "Free Lecture",
            "confidence": 0.9, "price__value": 0, "price__conf": 0.8,
        },
    ]
    scores = {dance_id: 0.8, free_talk_id: 0.2}

    ranked = rank(rows, f, tag_scores=scores)
    assert [row["title"] for row in ranked] == [
        "Dance Night", "Free Lecture",
    ]

    price_first = rank(
        rows, f,
        importance={"tags": 0.1, "price": 1.0},
        tag_scores=scores,
    )
    assert [row["title"] for row in price_first] == [
        "Free Lecture", "Dance Night",
    ]


def test_stronger_semantic_match_beats_a_large_secondary_price_difference():
    f = _filters(tags=["salsa", "dance"], preferred_max_price=20)
    salsa_id, cheap_generic_id = uuid.uuid4(), uuid.uuid4()
    rows = [
        {
            "event_id": salsa_id, "title": "Salsa Workshop",
            "confidence": 0.8, "price__value": 190, "price__conf": 0.9,
        },
        {
            "event_id": cheap_generic_id, "title": "Generic Dance",
            "confidence": 0.8, "price__value": 10, "price__conf": 0.3,
        },
    ]
    ranked = rank(
        rows, f, tag_scores={salsa_id: 0.54, cheap_generic_id: 0.46}
    )
    assert [row["title"] for row in ranked] == [
        "Salsa Workshop", "Generic Dance",
    ]


def test_min_tag_match_is_an_explicit_hard_filter():
    f = _filters(tags=["dance"], min_tag_match=0.6)
    salsa_id, chess_id = uuid.uuid4(), uuid.uuid4()
    rows = [
        {"event_id": salsa_id, "title": "Salsa", "confidence": 0.8},
        {"event_id": chess_id, "title": "Chess", "confidence": 0.9},
    ]
    ranked = rank(rows, f, tag_scores={salsa_id: 0.75, chess_id: 0.05})
    assert [row["title"] for row in ranked] == ["Salsa"]


# ------------------------------------------------------------- registry

def test_filter_defaults_cover_the_whole_model():
    assert set(FILTER_DEFAULTS) == set(SearchFilters.model_fields)


def test_registry_covers_every_soft_filter_field():
    soft = {"gender_split_min", "kid_friendly", "newcomer_friendly",
            "outdoor", "energy", "language", "age", "solo_friendly",
            "interaction_structure", "sex_service_context", "price",
            "event_scale"}
    assert soft == set(ATTRIBUTES)
    assert SOFT_ATTRIBUTES == soft | {"tags"}
    assert REQUIRED_ATTRIBUTES == SOFT_ATTRIBUTES - {"price", "tags"}


def test_unknown_required_attribute_is_rejected():
    with pytest.raises(ValidationError):
        _filters(required_attributes=["favourite_color"])
    with pytest.raises(ValidationError):
        _filters(preferred_max_price=30, required_attributes=["price"])
    with pytest.raises(ValidationError):
        _filters(tags=["dance"], required_attributes=["tags"])


def test_name_does_not_blur_event_title_with_venue(conn):
    _add(conn, "Community Oktoberfest", venue="factory300")
    _add(conn, "Sommerkonzert")
    titles = _run(conn, _filters(name="factory300"))
    assert titles == []


def test_literal_organizer_combines_with_concept_tags(conn):
    _add(conn, "Gründungsworkshop", organizer="WKO Oberösterreich",
         tags=["startup"])
    _add(conn, "Gründungsworkshop", organizer="Private Academy",
         tags=["startup"])
    _add(conn, "Export Seminar", organizer="WKO Oberösterreich",
         tags=["business"])
    conn.commit()
    titles = _run(conn, _filters(organizer="WKO", tags=["startup"]))
    assert titles[0] == "Gründungsworkshop"
    assert titles == ["Gründungsworkshop", "Export Seminar"]


def test_literal_reporting_source_combines_with_concept_tags(conn):
    source_id = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust) VALUES "
        "('Wirtschaftskammer Events', 'https://wko.at/events', "
        "'website', 2, 0.8) RETURNING id"
    ).fetchone()["id"]
    startup_id = _add(conn, "Gründungsworkshop", tags=["startup"])
    export_id = _add(conn, "Export Seminar", tags=["business"])
    for event_id in (startup_id, export_id):
        fingerprint = f"source-filter-{event_id}"
        conn.execute(
            "INSERT INTO event_claim (source_id, fingerprint, payload) "
            "VALUES (%s, %s, '{}')",
            (source_id, fingerprint),
        )
        conn.execute(
            "INSERT INTO identity (fingerprint, event_id) VALUES (%s, %s)",
            (fingerprint, event_id),
        )
    _add(conn, "Other Startup", tags=["startup"])
    conn.commit()
    titles = _run(conn, _filters(source="WKO", tags=["startup"]))
    assert titles == ["Gründungsworkshop", "Export Seminar"]


def test_exclude_terms_match_venue_and_spare_venueless(conn):
    _add(conn, "Community Oktoberfest", venue="factory300")
    _add(conn, "Sommerkonzert")  # no venue: must NOT be null-poisoned out
    titles = _run(conn, _filters(exclude_terms=["factory300"]))
    assert titles == ["Sommerkonzert"]


def test_multiword_name_matches_hyphen_and_compound(conn):
    _add(conn, "Linzer Krone-Fest 2026")
    _add(conn, "SoulSanity LIVE @ Linzer Kronefest")
    _add(conn, "Kronleuchter-Ausstellung")
    titles = _run(conn, _filters(name="krone fest"))
    assert set(titles) == {"Linzer Krone-Fest 2026",
                           "SoulSanity LIVE @ Linzer Kronefest"}


def test_price_and_event_scale_are_soft_by_default_and_hard_when_required(conn):
    _add(conn, "Small Free Meetup", price=0, attendance=30, attendance_conf=0.6)
    _add(conn, "Large Estimated Gala", estimated_price=25,
         attendance=600, attendance_conf=0.6)
    _add(conn, "Unknown Gathering")
    conn.commit()

    soft = _run(conn, _filters(
        preferred_max_price=30, participant_count_min=300,
    ))
    assert set(soft) == {
        "Small Free Meetup", "Large Estimated Gala", "Unknown Gathering"
    }
    hard = _run(conn, _filters(
        participant_count_min=300,
        required_attributes=["event_scale"],
    ))
    assert hard == ["Large Estimated Gala"]
