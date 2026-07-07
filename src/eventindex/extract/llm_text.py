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
- starts_at/ends_at: ISO 8601. If no time given, use the date alone (YYYY-MM-DD). \
Do not invent times, prices, or venues - omit unknown fields (null).
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
    from datetime import date

    from eventindex.extract import field

    if len(text.strip()) < 100:
        return []  # JS shell or empty page; headless rendering is phase 3

    prompt = _PROMPT.format(
        today=date.today().isoformat(),
        categories=", ".join(config.CATEGORIES),
        text=text[:MAX_CHARS],
    )
    result = llm.complete(
        tx, prompt, LLMExtraction,
        source_id=source["id"], job_id=job_id,
    )

    payloads = []
    for ev in result.events:
        conf = min(max(ev.confidence, 0.0), CONFIDENCE_CAP)
        fields = ev.model_dump(
            exclude_none=True, exclude={"confidence", "category", "recurrence"}
        )
        if ev.category in config.CATEGORIES:
            fields["category"] = ev.category
        if ev.recurrence is not None:
            # full dump, nulls kept: the stored claim must round-trip through
            # the strict Recurrence schema at resolve time
            fields["recurrence"] = ev.recurrence.model_dump()
        payloads.append({k: field(v, conf) for k, v in fields.items()})
    return payloads
