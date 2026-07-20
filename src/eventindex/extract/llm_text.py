"""Tier c: LLM extraction on readable page text (mini model, structured
output, budget-enforced through eventindex.llm)."""

from typing import Literal

from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict

from eventindex import config, llm
from eventindex.resolve.recurrence import Recurrence

MAX_CHARS = 20_000
CONFIDENCE_CAP = 0.9  # self-reported confidence is never taken at face value

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
- Copy titles VERBATIM (film titles: never add or drop version markers like \
OmdtU/DF yourself - keep exactly what the page shows).
- starts_at/ends_at: ISO 8601. If no time given, use the date alone (YYYY-MM-DD). \
Do not invent times, prices, or venues - omit unknown fields (null).
- organizer: the organizing club/company/person if the text names one.
- booking_url: a ticket/registration link if one appears as literal text.
- registration_required: true for "Anmeldung erforderlich/erbeten", false for \
"keine Anmeldung nötig"/"einfach vorbeikommen", else null.
- category: one of {categories}, or null.
- confidence: your certainty (0-1) that this is a real upcoming event with correct date.
- recurrence: ONLY if the text describes a repeating pattern ("jeden Dienstag", \
"wöchentlich", a course timetable row). Copy the exact wording into as_stated. \
For a repeating event, starts_at = the first upcoming occurrence. \
"außer Ferien"/"nicht in den Schulferien" -> except_holidays=["school_holidays"]. \
One-off events: recurrence=null.
- status: "cancelled" if marked ABGESAGT/abgesagt, "moved" if verschoben, else null.

PAGE TEXT:
{text}"""


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


class LLMExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[LLMEvent]


def html_to_text(content: bytes) -> str:
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def extract(tx, text: str, source: dict, job_id=None) -> list[dict]:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    if len(text.strip()) < 100:
        return []  # JS shell or empty page; headless rendering is phase 3

    prompt = _PROMPT.format(
        today=datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat(),
        categories=", ".join(config.CATEGORIES),
        text=text[:MAX_CHARS],
    )
    result = llm.complete(
        tx, prompt, LLMExtraction,
        source_id=source["id"], job_id=job_id,
    )
    return to_payloads(result)


def to_payloads(result: LLMExtraction) -> list[dict]:
    """LLMExtraction -> claim payloads: shared by the text, vision, and
    agent emit_events paths so validation/confidence rules exist once."""
    from eventindex.extract import field

    payloads = []
    for ev in result.events:
        if ev.confidence < 0.3:
            continue  # the model's own "probably not an event" (audit A23)
        conf = min(max(ev.confidence, 0.0), CONFIDENCE_CAP)
        fields = ev.model_dump(
            exclude_none=True, exclude={"confidence", "category", "recurrence"}
        )
        if ev.category in config.CATEGORIES:
            fields["category"] = ev.category
        if ev.recurrence is not None and ev.recurrence.freq not in ("once", "irregular"):
            # full dump, nulls kept: the stored claim must round-trip through
            # the strict Recurrence schema at resolve time. once/irregular is
            # the model saying "not actually recurring" - storing it would
            # mint a bogus series (and a 00:00 occurrence when time is null)
            fields["recurrence"] = ev.recurrence.model_dump()
        payloads.append({k: field(v, conf) for k, v in fields.items()})
    return payloads
