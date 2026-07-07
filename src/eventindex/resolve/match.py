"""Pairwise claim matching (§6): weighted score over title/time/venue/
organizer/url. Pure functions - the thresholds are deliberately biased high;
false-merge is the bad error (H2.3), the grey zone goes to LLM adjudication.
"""

from dataclasses import dataclass
from datetime import datetime

from eventindex.resolve.fingerprint import geo_cell, normalize_title

AUTO_MERGE = 0.80
GREY_ZONE = 0.50  # H2.3: keep the grey zone wide - adjudication is cheap,
                  # a silent near-miss duplicate is not (red-team: 0.542)
MID_ESCALATION = 0.65  # mini says "different" above this -> one mid-model re-ask

MERGE = "merge"
ADJUDICATE = "adjudicate"
DISTINCT = "distinct"


@dataclass
class Candidate:
    """The fields of a claim (group) that matter for matching."""

    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    venue_id: object = None
    lat: float | None = None
    lon: float | None = None
    organizer: str | None = None
    url: str | None = None
    has_time: bool = True  # False when the source gave a bare date


def _trigrams(s: str) -> set[str]:
    s = f"  {s} "  # pg_trgm-style padding
    return {s[i : i + 3] for i in range(len(s) - 2)}


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    ta, tb = _trigrams(na), _trigrams(nb)
    if not ta or not tb:
        return 0.0
    trigram = len(ta & tb) / len(ta | tb)
    # series-prefix variants ("Erwin Schrott" vs "Klassik am Dom 2026 -
    # Erwin Schrott") dilute trigram overlap; word containment restores
    # enough signal to reach the adjudicator - deliberately capped so
    # containment alone can never auto-merge (precision stays LLM-gated)
    wa, wb = set(na.split()), set(nb.split())
    shorter = min(len(wa), len(wb))
    containment = len(wa & wb) / shorter if shorter else 0.0
    # 0.72: even perfect containment + perfect venue/time stays < 0.80
    cap = 0.60 if shorter == 1 else 0.72
    return max(trigram, containment * cap)


def _time_overlap(a: Candidate, b: Candidate) -> float:
    if not (a.has_time and b.has_time):
        return 0.5  # unknown time is unknown, not a mismatch
    delta = abs((a.starts_at - b.starts_at).total_seconds()) / 60
    if delta <= 30:
        return 1.0
    if delta <= 180:
        return 0.4
    return 0.0


def _venue_match(a: Candidate, b: Candidate) -> float:
    if a.venue_id is not None and a.venue_id == b.venue_id:
        return 1.0
    if a.venue_id is not None and b.venue_id is not None:
        return 0.0  # both resolved, different venues
    cell_a, cell_b = geo_cell(a.lat, a.lon), geo_cell(b.lat, b.lon)
    if cell_a and cell_a == cell_b:
        return 0.8
    return 0.5  # at least one side unknown


def _organizer_match(a: Candidate, b: Candidate) -> float:
    if not a.organizer or not b.organizer:
        return 0.5
    return title_similarity(a.organizer, b.organizer)


def _url_overlap(a: Candidate, b: Candidate) -> float:
    if not a.url or not b.url:
        return 0.4
    if a.url.rstrip("/") == b.url.rstrip("/"):
        return 1.0
    domain_a = a.url.split("/")[2] if "//" in a.url else ""
    domain_b = b.url.split("/")[2] if "//" in b.url else ""
    # different sources link their own pages for the same event - weak prior,
    # not evidence against
    return 0.6 if domain_a and domain_a == domain_b else 0.4


def pair_score(a: Candidate, b: Candidate) -> float:
    return (
        0.35 * title_similarity(a.title, b.title)
        + 0.25 * _time_overlap(a, b)
        + 0.20 * _venue_match(a, b)
        + 0.10 * _organizer_match(a, b)
        + 0.10 * _url_overlap(a, b)
    )


def classify(score: float) -> str:
    if score > AUTO_MERGE:
        return MERGE
    if score >= GREY_ZONE:
        return ADJUDICATE
    return DISTINCT
