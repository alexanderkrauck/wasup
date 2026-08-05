"""Tier c: evidence-bound LLM extraction on readable page text.

The model proposes fields, but deterministic code decides whether the cited
source text actually supports them. Unsupported optional fields are dropped;
unsupported title/date identity drops the event.
"""

import html as html_lib
import re
import unicodedata
from datetime import datetime
from typing import Literal
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict

from eventindex import config, llm
from eventindex.resolve.recurrence import Recurrence

MAX_CHARS = 20_000
CONFIDENCE_CAP = 0.9  # self-reported confidence is never taken at face value
PROMPT_VERSION = 2

_PROMPT = """Extract all upcoming events from this web page text (usually German, \
from Linz, Austria). Today is {today}.

Rules:
- Only actual events/courses/happenings with a concrete date. Skip navigation, \
news without dates, and past events.
- title must identify the SPECIFIC act/program, never just the venue or a \
generic word ("Sandburg Events" is worthless). For series/festival slots, \
include the act: "Klassik am Dom: Tom Jones". If a listing gives no \
identifiable title at all, skip it.
- ONE event per happening: if a listing shows Einlass/doors AND Beginn/start, \
emit a single event with starts_at = Beginn (never two events for one show).
- Reopening/offer announcements are NOT events: "Wiedereröffnung - Touren ab \
13. Juli", "jetzt wieder geöffnet", "neue Öffnungszeiten" describe an ongoing \
offering. Only a specific dated celebration ("Wiedereröffnungsfeier am 5.9.") \
is an event.
- Ausstellungen (exhibitions): starts_at = opening date, ends_at = closing \
date ("bis 12.10.") when stated - the RANGE, never an arbitrary day mid-run.
- Copy titles VERBATIM (film titles: never add or drop version markers like \
OmdtU/DF yourself - keep exactly what the page shows).
- starts_at/ends_at: ISO 8601. If no time given, use the date alone (YYYY-MM-DD). \
Do not invent times, prices, or venues - omit unknown fields (null).
- A bare validity range on a recurring offer ("12.07. - 08.09.2026", \
"bis 16.08.") is NOT an occurrence: never use a range boundary as starts_at. \
Emit such a listing only if a concrete date, weekday or schedule is stated \
(then use recurrence with starts_at = the first stated occurrence).
- organizer: the organizing club/company/person if the text names one.
- booking_url: a ticket/registration link if one appears as literal text.
- registration_required: true for "Anmeldung erforderlich/erbeten", false for \
"keine Anmeldung nötig"/"einfach vorbeikommen", else null.
- category: one of {categories}, or null.
- confidence: your certainty (0-1) that this is a real upcoming event with correct date.
- recurrence: ONLY if the text describes a repeating pattern ("jeden Dienstag", \
"wöchentlich", a course timetable row) FOR THIS SPECIFIC event. Page-level or \
group wording (a site-wide "täglich geöffnet", an umbrella "täglich" over a \
list of per-weekday entries) is NOT this event's schedule - omit recurrence \
then. Copy the exact wording into as_stated. \
For a repeating event, starts_at = the first upcoming occurrence. \
"außer Ferien"/"nicht in den Schulferien" -> except_holidays=["school_holidays"]. \
One-off events: recurrence=null.
- status: "cancelled" if marked ABGESAGT/abgesagt, "moved" if verschoben, else null.
- evidence is mandatory. event_excerpt must be ONE verbatim contiguous passage
  (maximum 600 characters) containing this event's title and date together.
  title and starts_at must each quote the smallest verbatim supporting fragment
  inside event_excerpt. For every other non-null field, provide the smallest
  verbatim source fragment supporting it in the matching evidence field. Never
  cite text from another event. If title/date cannot be supported this way,
  skip the event. If an optional field has no literal support, return it null.
- field_confidences is mandatory. Give title and starts_at their own certainty
  and give every other non-null field its own certainty (0-1); use null for
  null fields. Confidence means certainty that this exact field value is
  supported by its cited source fragment.

PAGE TEXT:
{text}"""


class LLMEventEvidence(BaseModel):
    """Verbatim source spans corresponding to LLMEvent fields."""

    model_config = ConfigDict(extra="forbid")
    event_excerpt: str
    title: str
    starts_at: str
    ends_at: str | None
    venue_name: str | None
    address: str | None
    description: str | None
    url: str | None
    price_min: str | None
    price_max: str | None
    category: str | None
    organizer: str | None
    booking_url: str | None
    registration_required: str | None
    recurrence: str | None
    status: str | None


class LLMFieldConfidences(BaseModel):
    """Per-field certainty; text extraction requires one for every value."""

    model_config = ConfigDict(extra="forbid")
    title: float
    starts_at: float
    ends_at: float | None
    venue_name: float | None
    address: float | None
    description: float | None
    url: float | None
    price_min: float | None
    price_max: float | None
    category: float | None
    organizer: float | None
    booking_url: float | None
    registration_required: float | None
    recurrence: float | None
    status: float | None


class LLMEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    starts_at: str
    ends_at: str | None
    venue_name: str | None
    address: str | None
    description: str | None
    url: str | None
    price_min: float | None
    price_max: float | None
    category: str | None
    organizer: str | None
    booking_url: str | None
    registration_required: bool | None
    recurrence: Recurrence | None
    status: Literal["cancelled", "moved", "postponed"] | None
    confidence: float
    # Optional at the shared schema boundary because vision and the tier-D
    # browser agent use this model too. Text extraction below requires and
    # validates it; those modalities retain their existing validation path.
    evidence: LLMEventEvidence | None = None
    field_confidences: LLMFieldConfidences | None = None


class LLMTextEvent(LLMEvent):
    """Text-tier event: evidence and field certainties are schema-required."""

    evidence: LLMEventEvidence
    field_confidences: LLMFieldConfidences


class LLMExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[LLMEvent]


class LLMTextExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[LLMTextEvent]


def html_to_text(content: bytes, base_url: str | None = None) -> str:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # Preserve literal public links in the text the model sees. This makes a
    # relative booking/detail link evidence-verifiable without teaching the
    # extractor anything about a particular website.
    if base_url:
        for anchor in soup.find_all("a", href=True):
            absolute = urljoin(base_url, anchor["href"])
            if absolute.startswith(("http://", "https://")):
                anchor.append(f" [{absolute}]")
    return " ".join(soup.get_text(" ").split())


def _norm(value: str) -> str:
    return " ".join(
        unicodedata.normalize("NFKC", html_lib.unescape(value)).split()
    ).casefold()


def _is_literal_quote(quote: str | None, source_text: str) -> bool:
    return bool(quote and _norm(quote) in _norm(source_text))


_TIME_RE = re.compile(
    r"(?<!\d)(?P<hour>[01]?\d|2[0-3])"
    r"(?::(?P<colon_minute>[0-5]\d)"
    r"|\.(?P<dot_minute>[0-5]\d)(?![./-]\d)|\s*uhr)\b",
    re.IGNORECASE,
)


def _date_evidence_matches(value: str, quote: str) -> tuple[bool, str]:
    """Return whether a cited fragment supports the claimed date/time.

    A source that states only a date can support only a date-only claim. The
    normalized value is returned so an invented midnight/time is not stored.
    Regex here only recognizes numeric clock syntax; it makes no semantic
    content judgment.
    """
    from dateutil import parser as dateparser

    from eventindex.extract import _GERMAN_DATE_PARTS, parse_dt

    claimed = parse_dt(value)
    if claimed is None:
        return False, value
    local = claimed.astimezone(ZoneInfo(config.TIMEZONE))
    normalized = quote.removesuffix(" Uhr")
    for german, english in _GERMAN_DATE_PARTS:
        normalized = normalized.replace(german, english)
    # dateutil's dayfirst mode reverses the month/day of an otherwise
    # unambiguous ISO quote (2026-09-04 became 2026-04-09).  Machine-readable
    # source dates are common evidence, so preserve their explicit order.
    iso_quote = bool(re.search(
        r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)", normalized,
    ))
    try:
        cited = dateparser.parse(
            normalized,
            dayfirst=not iso_quote,
            yearfirst=iso_quote,
            fuzzy=True,
            default=local.replace(hour=0, minute=0, second=0, microsecond=0),
        )
    except (ValueError, OverflowError):
        return False, value
    if cited is None or cited.date() != local.date():
        return False, value

    clock = _TIME_RE.search(quote)
    claimed_has_time = "T" in value or bool(re.search(r"\d\s+\d{1,2}:", value))
    if clock is None:
        return True, local.date().isoformat()
    minute = int(
        clock.group("colon_minute") or clock.group("dot_minute") or 0
    )
    if not claimed_has_time:
        return False, value
    if (local.hour, local.minute) != (int(clock.group("hour")), minute):
        return False, value
    return True, value


def _supported_optional(
    name: str, value, quote: str | None, source_text: str,
) -> tuple[bool, object]:
    if not _is_literal_quote(quote, source_text):
        return False, value
    if name in ("ends_at",):
        return _date_evidence_matches(str(value), quote or "")
    if name in (
        "venue_name", "address", "description", "url", "organizer",
        "booking_url",
    ):
        return _norm(str(value)) in _norm(quote or ""), value
    if name in ("price_min", "price_max"):
        numbers = [
            float(match.replace(",", "."))
            for match in re.findall(r"(?<!\d)\d+(?:[.,]\d+)?", quote or "")
        ]
        try:
            return any(abs(float(value) - number) < 0.01 for number in numbers), value
        except (TypeError, ValueError):
            return False, value
    # Category, price, booleans, status and recurrence are interpretations of
    # a cited span. Their ordinary schema/sanity validators still apply.
    return True, value


def extract(
    tx, text: str, source: dict, job_id=None, observed_url: str | None = None,
) -> list[dict]:

    if len(text.strip()) < 100:
        return []  # JS shell or empty page; headless rendering is phase 3

    prompt = _PROMPT.format(
        today=datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat(),
        categories=", ".join(config.CATEGORIES),
        text=text[:MAX_CHARS],
    )
    result = llm.complete(
        tx, prompt, LLMTextExtraction,
        source_id=source["id"], job_id=job_id,
    )
    return to_payloads(
        result, source_text=text, observed_url=observed_url,
        basis="llm_text",
    )


def to_payloads(
    result: LLMExtraction, *, source_text: str | None = None,
    observed_url: str | None = None, basis: str = "llm",
) -> list[dict]:
    """LLMExtraction -> claim payloads: shared by the text, vision, and
    agent emit_events paths so validation/confidence rules exist once."""
    from eventindex.extract import field

    payloads = []
    for ev in result.events:
        if ev.confidence < 0.3:
            continue  # the model's own "probably not an event" (audit A23)
        conf = min(max(ev.confidence, 0.0), CONFIDENCE_CAP)
        fields = ev.model_dump(
            exclude_none=True,
            exclude={
                "confidence", "category", "recurrence", "evidence",
                "field_confidences",
            },
        )
        if ev.category in config.CATEGORIES:
            fields["category"] = ev.category
        if ev.recurrence is not None and ev.recurrence.freq not in ("once", "irregular"):
            # full dump, nulls kept: the stored claim must round-trip through
            # the strict Recurrence schema at resolve time. once/irregular is
            # the model saying "not actually recurring" - storing it would
            # mint a bogus series (and a 00:00 occurrence when time is null)
            fields["recurrence"] = ev.recurrence.model_dump()
        evidence = ev.evidence
        field_confidences = ev.field_confidences
        if evidence is None or field_confidences is None:
            continue
        excerpt = evidence.event_excerpt[:600]
        title_quote = evidence.title
        starts_quote = evidence.starts_at
        if not (
            (source_text is None or _is_literal_quote(excerpt, source_text))
            and _is_literal_quote(title_quote, excerpt)
            and _is_literal_quote(starts_quote, excerpt)
            and _norm(ev.title) in _norm(title_quote)
        ):
            continue
        date_ok, normalized_start = _date_evidence_matches(
            ev.starts_at, starts_quote,
        )
        if not date_ok:
            continue
        fields["starts_at"] = normalized_start

        kept: dict = {}
        for name, value in fields.items():
            quote = getattr(evidence, name, None)
            if name == "title":
                quote = title_quote
                ok, normalized = True, value
            elif name == "starts_at":
                quote = starts_quote
                ok, normalized = True, value
            else:
                ok, normalized = _supported_optional(
                    name, value, quote,
                    source_text if source_text is not None else (quote or ""),
                )
            if not ok:
                continue
            field_confidence = getattr(field_confidences, name, None)
            if field_confidence is None:
                continue
            kept[name] = field(
                normalized,
                min(max(field_confidence, 0.0), conf),
                evidence=quote,
                basis=basis,
                observed_url=observed_url,
                evidence_version=1,
                prompt_version=PROMPT_VERSION,
                **({"event_excerpt": excerpt} if name == "title" else {}),
            )
        if "title" in kept and "starts_at" in kept:
            payloads.append(kept)
    return payloads
