"""Gap detection + horizon-gated projection (pure functions), and the §9b
private-intent heuristic."""

from datetime import date

from eventindex.resolve.projection import detect_cadence, project
from eventindex.resolve.rebuild import _is_private_intent


def _weekly(n, start=date(2026, 6, 3), step=7):
    from datetime import timedelta

    return [start + timedelta(days=i * step) for i in range(n)]


def test_weekly_detected():
    assert detect_cadence(_weekly(4)) == 7


def test_biweekly_detected():
    assert detect_cadence(_weekly(4, step=14)) == 14


def test_one_day_jitter_tolerated():
    days = [date(2026, 6, 3), date(2026, 6, 10), date(2026, 6, 18)]  # 7, 8
    assert detect_cadence(days) == 7


def test_single_holiday_skip_tolerated():
    days = [date(2026, 6, 3), date(2026, 6, 10), date(2026, 6, 24),
            date(2026, 7, 1)]  # one missing beat (Jun 17)
    assert detect_cadence(days) == 7


def test_irregular_is_not_projectable():
    days = [date(2026, 6, 3), date(2026, 6, 8), date(2026, 6, 24)]
    assert detect_cadence(days) is None


def test_two_dates_are_not_a_series():
    assert detect_cadence(_weekly(2)) is None


def test_two_skips_rejected():
    days = [date(2026, 6, 3), date(2026, 6, 17), date(2026, 7, 1)]  # both 14
    assert detect_cadence(days) != 7  # that is a clean biweekly instead
    assert detect_cadence(days) == 14


def test_projection_starts_beyond_coverage_edge():
    days = _weekly(4)  # Jun 3, 10, 17, 24
    # feed demonstrably reaches Jul 5: Jul 1 would have been visible -> its
    # absence is evidence, only later beats are projected
    out = project(days, 7, coverage_edge=date(2026, 7, 5))
    assert out == [date(2026, 7, 8), date(2026, 7, 15), date(2026, 7, 22)]


def test_projection_capped_at_four_weeks_past_last_observation():
    out = project(_weekly(4), 7, coverage_edge=date(2026, 6, 24))
    assert out[-1] <= date(2026, 7, 22)  # last observed Jun 24 + 4 weeks
    assert len(out) == 4


def test_projection_empty_when_feed_covers_everything():
    assert project(_weekly(4), 7, coverage_edge=date(2026, 9, 1)) == []


# ---------------------------------------------------------------- §9b

def test_residential_address_with_personal_organizer_is_private():
    assert _is_private_intent("Wildbergstraße 18a, 4040 Linz", "Maria Huber")


def test_org_organizer_is_not_private():
    assert not _is_private_intent("Wildbergstraße 18, Linz", "Kulturverein Zeit")
    assert not _is_private_intent("Hauptplatz 1, Linz", "Stadt Linz")


def test_venueless_address_without_house_number_is_not_private():
    assert not _is_private_intent("Donaulände, Linz", "Maria Huber")


def test_missing_signals_are_not_private():
    assert not _is_private_intent(None, "Maria Huber")
    assert not _is_private_intent("Wildbergstraße 18, Linz", None)
