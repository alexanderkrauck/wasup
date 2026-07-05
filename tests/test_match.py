from datetime import datetime, timezone

from eventindex.resolve.match import (
    ADJUDICATE, DISTINCT, MERGE, Candidate, classify, pair_score,
    title_similarity,
)

T = datetime(2026, 7, 10, 20, 0, tzinfo=timezone.utc)


def _cand(**kw):
    base = dict(title="Chet Faker", starts_at=T, venue_id="v1")
    base.update(kw)
    return Candidate(**base)


def test_same_title_same_venue_same_time_automerges():
    score = pair_score(_cand(), _cand(url="https://a.at/x"))
    assert classify(score) == MERGE


def test_title_variant_lands_in_grey_zone_not_automerge():
    score = pair_score(_cand(), _cand(title="Chet Faker Live 2026"))
    assert classify(score) == ADJUDICATE


def test_resolved_different_venues_blocks_automerge():
    score = pair_score(_cand(), _cand(venue_id="v2"))
    assert classify(score) != MERGE


def test_unrelated_events_distinct():
    score = pair_score(
        _cand(),
        _cand(title="Flohmarkt der Pfarre", venue_id="v2",
              starts_at=T.replace(hour=9)),
    )
    assert classify(score) == DISTINCT


def test_unknown_time_is_neutral_not_negative():
    with_time = pair_score(_cand(), _cand(starts_at=T.replace(hour=9)))
    unknown = pair_score(_cand(), _cand(has_time=False))
    assert unknown > with_time


def test_title_similarity_normalizes():
    assert title_similarity("GRÜNMARKT Urfahr!", "Gruenmarkt Urfahr") == 1.0
