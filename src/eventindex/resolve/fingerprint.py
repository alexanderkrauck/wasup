"""Blocking fingerprint (§6): normalize(title) + date_bucket + geo_cell.

Venue-name stripping needs the venue table (phase 2); until then the title
normalization is lowercase + umlaut folding + date/stopword removal.
"""

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from eventindex import config

VIENNA = ZoneInfo(config.TIMEZONE)

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_STOPWORDS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "und", "mit",
    "im", "in", "am", "an", "um", "auf", "bei", "fuer", "von", "vom", "zum",
    "zur", "the", "a", "an", "and", "of", "at", "with",
}


def normalize_title(title: str) -> str:
    s = title.lower().translate(_UMLAUTS)
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"\d+", " ", s)  # strips dates, times, years
    s = re.sub(r"[^a-z]+", " ", s)
    words = [w for w in s.split() if w not in _STOPWORDS]
    return " ".join(words)


def geo_cell(lat: float | None, lon: float | None) -> str:
    """~500m grid cell (at Linz latitude)."""
    if lat is None or lon is None:
        return ""
    return f"{round(lat * 200)}:{round(lon * 133)}"


def fingerprint(
    title: str,
    starts_at: datetime,
    *,
    lat: float | None = None,
    lon: float | None = None,
    venue_id=None,
) -> str:
    date_bucket = starts_at.astimezone(VIENNA).date().isoformat()
    cell = str(venue_id) if venue_id else geo_cell(lat, lon)
    return f"{normalize_title(title)}|{date_bucket}|{cell}"
