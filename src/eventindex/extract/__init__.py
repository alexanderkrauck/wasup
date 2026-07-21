"""Extraction cascade (§3.3): stop at the first tier that yields events.

  a. JSON API record sniffing   (free, precise; fence fired 2026-07-20)
  b. JSON-LD schema.org/Event   (free, precise)
  c. ICS / RSS                  (free, precise)
  d. PDF text -> LLM            (fence fired 2026-07-20)
  e. LLM on readable page text  (mini model, structured output)

Every tier emits claim payloads: {field: {"value": ..., "confidence": float}}.
"""

import html as _html
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dateparser

from eventindex import config
from eventindex.extract import ics, json_api, jsonld, linztermine, llm_text, pdf, rss

VIENNA = ZoneInfo(config.TIMEZONE)


def field(value, confidence: float) -> dict:
    return {"value": value, "confidence": confidence}


# ---- string hygiene: ONE choke point (audit A6/A18/A22). Applied both at
# extraction (sanity_filter) and at rebuild (_load_claims), so the immutable
# claim history is repaired for free on the next resolve.

_DECOR_RE = re.compile(r"[\U0001F000-\U0001FAFF☀-➿❤️]")
_CLICKBAIT_RE = re.compile(
    r"\s*[-–—|/]*\s*(fast ausverkauft|ausverkauft|tickets? (jetzt )?sichern"
    r"|jetzt tickets?( kaufen)?)\s*$",
    re.IGNORECASE,
)
_STRING_FIELDS = ("title", "description", "venue_name", "address",
                  "organizer", "url", "booking_url")
_EXPLICIT_EVENT_START_RE = re.compile(
    r"\b(?:konzert|veranstaltungs|vorstellungs)beginn\s*:?\s*"
    r"(?P<hour>\d{1,2})[:.](?P<minute>\d{2})(?:\s*uhr)?\b",
    re.IGNORECASE,
)


def clean_text(value: str) -> str:
    """Sources ship double-escaped entities (&amp;amp;) - 75 titles reached
    prod with &quot; in them. Unescape twice, collapse whitespace."""
    return " ".join(_html.unescape(_html.unescape(value)).split())


def _clean_title(title: str, venue_name: str | None) -> str:
    t = _CLICKBAIT_RE.sub("", _DECOR_RE.sub(" ", title))
    t = " ".join(t.split())
    # extractors concatenate the venue onto the title ("KinderUni Linz Linz
    # Innenstadt", "... - Posthof Linz"): strip a trailing venue mention
    if venue_name:
        low, vlow = t.lower(), venue_name.lower().strip()
        for suffix in (vlow, f"{vlow} linz", f"- {vlow}", f"- {vlow} linz"):
            if low.endswith(suffix) and len(t) - len(suffix) >= 4:
                t = t[: len(t) - len(suffix)].rstrip(" -–—|,/")
                break
    return t


_YEAR_LIKE = range(1900, 2101)


def normalize_claim(payload: dict) -> dict:
    """In-place hygiene for one claim payload: entity-unescape all strings,
    de-decorate the title, drop implausible prices (a charity sale reached
    prod at EUR 1840 - the year as a price, audit A5)."""
    for key in _STRING_FIELDS:
        entry = payload.get(key)
        if entry and isinstance(entry.get("value"), str):
            entry["value"] = clean_text(entry["value"])
    title = payload.get("title", {}).get("value")
    if title:
        venue = (payload.get("venue_name") or {}).get("value")
        payload["title"]["value"] = _clean_title(title, venue)

    # 65 events shipped "Keine URL", bare domains, an e-mail address and an
    # umlaut domain as their link (red team 2026-07-21): a url either parses
    # as http(s) or it is not a url
    for key in ("url", "booking_url"):
        entry = payload.get(key)
        if entry is not None and not str(entry.get("value") or "").startswith(
            ("http://", "https://")
        ):
            payload.pop(key)

    # Some listings put box-office/doors time in their structured start
    # while stating the actual performance time unambiguously in prose
    # (live: 19:00 Abendkasse, 19:30 Einlass, Konzertbeginn 20:00). The
    # explicit event-start label wins; this also repairs immutable history
    # when normalize_claim runs during a canon rebuild.
    start_entry = payload.get("starts_at")
    description = (payload.get("description") or {}).get("value") or ""
    match = _EXPLICIT_EVENT_START_RE.search(description)
    if start_entry and match:
        raw_start = str(start_entry.get("value") or "")
        try:
            starts = dateparser.parse(raw_start)
            if starts.tzinfo is None:
                starts = starts.replace(tzinfo=VIENNA)
            local = starts.astimezone(VIENNA)
            explicit = local.replace(
                hour=int(match.group("hour")),
                minute=int(match.group("minute")), second=0, microsecond=0,
            )
            date_only = "T" not in raw_start and " " not in raw_start
            if (date_only or abs(explicit - local) <= timedelta(hours=6)) \
                    and explicit != local:
                start_entry["value"] = explicit.isoformat()
        except (ValueError, OverflowError):
            pass

    prices = {}
    for key in ("price_min", "price_max"):
        entry = payload.get(key)
        if entry is None or entry.get("value") is None:
            continue
        try:
            prices[key] = float(entry["value"])
        except (TypeError, ValueError):
            payload.pop(key)
    haystack = f"{title or ''} {(payload.get('description') or {}).get('value') or ''}"
    for key, p in prices.items():
        year_like = p == int(p) and int(p) in _YEAR_LIKE and str(int(p)) in haystack
        if p < 0 or p > 500 or year_like:
            payload.pop(key, None)
    pmin = (payload.get("price_min") or {}).get("value")
    pmax = (payload.get("price_max") or {}).get("value")
    if pmin is not None and pmax is not None and float(pmin) > float(pmax):
        payload.pop("price_min", None)
        payload.pop("price_max", None)
    return payload


# closures/holiday notices/program pointers/announcements are not events
# (audit A23; red team 2026-07-20: "Wiedereröffnung ... Touren ab 13. Juli"
# served as a future event. German compounding keeps this safe: a real
# "Wiedereröffnungsfeier" is one word and never matches \bwiedereröffnung\b).
_NON_EVENT_RE = re.compile(
    r"(sommerferien|weihnachtsferien|semesterferien|ferienbeginn|schulfrei"
    r"|hinweis auf|öffnungszeiten|betriebsurlaub|geschlossen|entfällt"
    r"|kein training|kein kurs|keine probe"
    r"|\bwiedereröffnung\b|\bneueröffnung\b|jetzt (wieder )?geöffnet)",
    re.IGNORECASE,
)


def is_non_event(title: str) -> bool:
    return bool(_NON_EVENT_RE.search(title))


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
    # digits survive normalization now; a year is not meaning ("Programm 2026")
    meaningful = {w for w in words if not w.isdigit()} \
        - _GENERIC_TITLE_WORDS - source_words
    return not meaningful


def sanity_filter(claims: list[dict], source: dict) -> list[dict]:
    """The deterministic gates every claim passes regardless of how it was
    extracted (cascade or recipe selectors): parseable future date, no
    placeholder title."""
    kept = []
    for c in claims:
        c = normalize_claim(c)
        if not is_upcoming(c):
            continue
        title = c.get("title", {}).get("value") or ""
        if is_placeholder_title(title, source.get("name") or ""):
            continue
        if is_non_event(title):
            continue
        kept.append(c)
    return kept


def extract(source: dict, result, tx, job_id=None) -> tuple[str, list[dict]]:
    """Run the cascade. Returns (method, claim payloads), past events dropped."""
    ct = result.content_type.lower()
    kind = source["kind"]

    # body sniff, not content-type: recipe fetches fake text/html, and SPA
    # platforms serve JSON under whatever header. XML/ICS bodies never start
    # like JSON, so the legacy branches below are unaffected.
    if "json" in ct or result.content.lstrip()[:1] in (b"{", b"["):
        if claims := json_api.parse(result.content):
            return "json_api", sanity_filter(claims, source)

    if pdf.is_pdf(result.content, ct):
        # text-layer PDFs feed the LLM tier; a scanned PDF yields no text
        # and returns empty here - the agent's vision path is its ladder rung
        claims = llm_text.extract(tx, pdf.to_text(result.content), source,
                                  job_id=job_id)
        return "pdf", sanity_filter(claims, source)

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
