"""Forward projection of implicit series (completeness contract, 2026-07-07).

Sources like the linztermine feed list "Zumba every Wednesday" as bare dates
inside a hard 7-day window; without projection the series appears to end at
the feed's horizon. Pure deterministic gap detection - no LLM.

The honesty gate: a date the feed COULD have shown but didn't is evidence of
absence, never a gap to fill. Only dates beyond the coverage edge (how far
the source's feed demonstrably reaches) are projected, and they are flagged
`projected` all the way to the API.
"""

from datetime import date, timedelta

MIN_DATES = 3          # matches EXPLICIT_SERIES_MIN_DATES in rebuild
CADENCES = (7, 14)     # weekly, biweekly; anything else is not projectable
TOLERANCE_DAYS = 1
PROJECTION_WEEKS = 4   # never project further than this past the last observation


def detect_cadence(days: list[date]) -> int | None:
    """Weekly/biweekly cadence of observed days, or None.

    One missed beat (a single gap of 2x the cadence - holiday skip) is
    tolerated; anything more irregular is not a projectable series.
    """
    days = sorted(set(days))
    if len(days) < MIN_DATES:
        return None
    gaps = [(b - a).days for a, b in zip(days, days[1:])]
    for cadence in CADENCES:
        on_beat = sum(1 for g in gaps if abs(g - cadence) <= TOLERANCE_DAYS)
        skipped = sum(1 for g in gaps if abs(g - 2 * cadence) <= TOLERANCE_DAYS)
        if on_beat + skipped == len(gaps) and skipped <= 1 and on_beat >= skipped:
            return cadence
    return None


def project(days: list[date], cadence: int, coverage_edge: date) -> list[date]:
    """Future dates continuing the cadence: strictly after the coverage edge
    (absence within it is evidence), at most PROJECTION_WEEKS past the last
    observed date."""
    days = sorted(set(days))
    last = days[-1]
    limit = last + timedelta(weeks=PROJECTION_WEEKS)
    out = []
    d = last + timedelta(days=cadence)
    while d <= limit:
        if d > coverage_edge:
            out.append(d)
        d += timedelta(days=cadence)
    return out
