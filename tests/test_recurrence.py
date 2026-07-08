"""The recurrence compiler/expander - the highest-value test target
(CLAUDE.md): wrong occurrences = trust dead."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from eventindex.resolve.recurrence import (
    Recurrence, compile_rrule, expand, series_fingerprint,
)

VIENNA = ZoneInfo("Europe/Vienna")

HOLIDAYS = {
    "school_holidays": [
        (date(2026, 7, 11), date(2026, 9, 13)),   # Sommerferien
        (date(2026, 10, 27), date(2026, 10, 31)),  # Herbstferien
    ],
    "public_holidays": [(date(2026, 8, 15), date(2026, 8, 15))],
}


def _rec(**kw):
    base = dict(
        freq="weekly", weekday="TU", week_of_month=None, interval=1,
        time="18:30", duration_minutes=90, except_holidays=[],
        valid_from=None, valid_until=None, as_stated="jeden Dienstag 18:30",
    )
    base.update(kw)
    return Recurrence(**base)


def test_weekly_skips_school_holidays():
    rec = _rec(except_holidays=["school_holidays"], valid_from="2026-09-01")
    now = datetime(2026, 9, 1, 0, 0, tzinfo=VIENNA)
    occs = [s for s, _ in expand(rec, HOLIDAYS, now=now)]
    days = [o.date() for o in occs]
    assert date(2026, 9, 8) not in days   # still Sommerferien
    assert date(2026, 9, 15) in days      # first Tuesday after
    assert date(2026, 10, 27) not in days  # Herbstferien Tuesday
    assert all(o.weekday() == 1 and o.hour == 18 and o.minute == 30 for o in occs)


def test_ends_at_from_duration():
    rec = _rec(valid_from="2026-09-15")
    now = datetime(2026, 9, 14, tzinfo=VIENNA)
    starts, ends = expand(rec, HOLIDAYS, now=now)[0]
    assert (ends - starts).total_seconds() == 90 * 60


def test_biweekly_keeps_phase_anchored_at_valid_from():
    rec = _rec(interval=2, valid_from="2026-09-15", except_holidays=[])
    now = datetime(2026, 9, 14, tzinfo=VIENNA)
    days = [s.date() for s, _ in expand(rec, HOLIDAYS, now=now)]
    assert days[:3] == [date(2026, 9, 15), date(2026, 9, 29), date(2026, 10, 13)]


def test_biweekly_without_valid_from_phase_locks_to_anchor_not_now():
    """Regression (STWST Treibgut, 2026-07-07): without valid_from, an
    interval-2 rule must take its phase from the claim's real occurrence,
    not from whenever the rebuild happens to run."""
    rec = _rec(freq="weekly", weekday="SA", interval=2, valid_from=None,
               except_holidays=[])
    # rebuild runs Mon Sep 21; the known real occurrence was Sat Sep 19
    now = datetime(2026, 9, 21, tzinfo=VIENNA)
    anchor = datetime(2026, 9, 19, 16, 0, tzinfo=VIENNA)
    days = [s.date() for s, _ in expand(rec, HOLIDAYS, now=now, anchor=anchor)]
    assert days[:2] == [date(2026, 10, 3), date(2026, 10, 17)]  # NOT Sep 26/Oct 10


def test_valid_until_cuts_off():
    rec = _rec(valid_from="2026-09-15", valid_until="2026-09-30")
    now = datetime(2026, 9, 14, tzinfo=VIENNA)
    days = [s.date() for s, _ in expand(rec, HOLIDAYS, now=now)]
    assert days == [date(2026, 9, 15), date(2026, 9, 22), date(2026, 9, 29)]


def test_irregular_creates_no_occurrences():
    rec = _rec(freq="irregular", as_stated="wenn Franz Zeit hat")
    assert expand(rec, HOLIDAYS, now=datetime(2026, 9, 1, tzinfo=VIENNA)) == []


def test_once():
    rec = _rec(freq="once", valid_from="2026-09-20", time="10:00")
    occs = expand(rec, HOLIDAYS, now=datetime(2026, 9, 15, tzinfo=VIENNA))
    assert len(occs) == 1
    assert occs[0][0] == datetime(2026, 9, 20, 10, 0, tzinfo=VIENNA)


def test_monthly_by_weekday():
    rec = _rec(freq="monthly_by_weekday", weekday="FR", week_of_month=1,
               valid_from="2026-09-01")
    now = datetime(2026, 9, 1, tzinfo=VIENNA)
    days = [s.date() for s, _ in expand(rec, HOLIDAYS, now=now)]
    assert days[:2] == [date(2026, 9, 4), date(2026, 10, 2)]  # first Fridays


def test_weekly_without_weekday_is_not_expandable():
    rec = _rec(weekday=None)
    assert compile_rrule(rec, datetime(2026, 9, 1, tzinfo=VIENNA)) is None


def test_series_fingerprint_tolerates_30min_and_matches_weekday():
    d1 = datetime(2026, 9, 15, 18, 30, tzinfo=VIENNA)
    d2 = datetime(2026, 9, 22, 18, 45, tzinfo=VIENNA)  # next week, 15min later
    d3 = datetime(2026, 9, 16, 18, 30, tzinfo=VIENNA)  # Wednesday
    assert series_fingerprint("Spinning", "v1", d1) == series_fingerprint(
        "SPINNING!", "v1", d2
    )
    assert series_fingerprint("Spinning", "v1", d1) != series_fingerprint(
        "Spinning", "v1", d3
    )


def test_valid_from_accepts_datetime_strings():
    """Models hand back datetimes where the schema asks for dates; cached
    values persist, so the parse must be lenient or rebuilds crash forever."""
    from datetime import datetime, timezone

    from eventindex.resolve.recurrence import Recurrence, expand

    rec = Recurrence(
        freq="weekly", weekday="FR", week_of_month=None, interval=1,
        time="08:00", duration_minutes=None, except_holidays=[],
        valid_from="2026-07-03T08:00:00", valid_until=None,
        as_stated="jeden Freitag ab 3.7.",
    )
    pairs = expand(rec, {"public_holidays": [], "school_holidays": []},
                   now=datetime(2026, 7, 8, tzinfo=timezone.utc))
    assert pairs and pairs[0][0].weekday() == 4  # Fridays, no crash
