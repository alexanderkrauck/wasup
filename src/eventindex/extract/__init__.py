"""Extraction cascade (§3.3): stop at the first tier that yields events.

  a. JSON-LD schema.org/Event   (free, precise)
  b. ICS / RSS                  (free, precise)
  c. LLM on readable page text  (mini model, structured output)

Every tier emits claim payloads: {field: {"value": ..., "confidence": float}}.
Vision/PDF (tier d) is out of v1 scope.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser

from eventindex import config
from eventindex.extract import ics, jsonld, linztermine, llm_text, rss

VIENNA = ZoneInfo(config.TIMEZONE)


def field(value, confidence: float) -> dict:
    return {"value": value, "confidence": confidence}


def parse_dt(value) -> datetime | None:
    """Parse a date(time); naive values are interpreted as Europe/Vienna."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = dateparser.parse(str(value))
        except (ValueError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=VIENNA)
    return dt


def is_upcoming(payload: dict) -> bool:
    """Deterministic sanity gate: starts_at parses and isn't in the past."""
    starts = payload.get("starts_at")
    dt = parse_dt(starts["value"]) if starts else None
    if dt is None:
        return False
    return dt >= datetime.now(timezone.utc) - timedelta(days=1)


_GENERIC_TITLE_WORDS = {
    "event", "events", "veranstaltung", "veranstaltungen", "termin",
    "termine", "programm", "kalender", "highlights",
}


def is_placeholder_title(title: str, source_name: str) -> bool:
    """Red-team finding 2026-07-07: venue programs collapsing into rows like
    "Sandburg Events" fake coverage while losing every event identity. A
    title is a placeholder when, after removing generic event-words, nothing
    remains but (parts of) the source's own name."""
    from eventindex.resolve.fingerprint import normalize_title

    words = set(normalize_title(title).split())
    if not words:
        return True
    source_words = set(normalize_title(source_name).split())
    meaningful = words - _GENERIC_TITLE_WORDS - source_words
    return not meaningful


def sanity_filter(claims: list[dict], source: dict) -> list[dict]:
    """The deterministic gates every claim passes regardless of how it was
    extracted (cascade or recipe selectors): parseable future date, no
    placeholder title."""
    kept = []
    for c in claims:
        if not is_upcoming(c):
            continue
        title = c.get("title", {}).get("value") or ""
        if is_placeholder_title(title, source.get("name") or ""):
            continue
        kept.append(c)
    return kept


def extract(source: dict, result, tx, job_id=None) -> tuple[str, list[dict]]:
    """Run the cascade. Returns (method, claim payloads), past events dropped."""
    ct = result.content_type.lower()
    kind = source["kind"]

    if kind == "api":
        # the only v1 API source is the linztermine XML export; a second API
        # format is the trigger for generalizing this branch
        method, claims = "linztermine_xml", linztermine.parse(result.content)
    elif kind == "ics" or "calendar" in ct:
        method, claims = "ics", ics.parse(result.content)
    elif kind == "rss" or "rss" in ct or "atom" in ct or "xml" in ct:
        claims = rss.parse(result.content)
        if claims:
            method = "rss"
        else:
            method = "llm"
            claims = llm_text.extract(
                tx, rss.to_text(result.content), source, job_id=job_id
            )
    else:
        claims = jsonld.parse(result.content, base_url=result.url)
        if claims:
            method = "jsonld"
        else:
            method = "llm"
            claims = llm_text.extract(
                tx, llm_text.html_to_text(result.content), source, job_id=job_id
            )

    return method, sanity_filter(claims, source)
