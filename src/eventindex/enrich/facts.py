"""Generic public-page recovery for action-critical event facts.

The extractor is deliberately event-anchored: it receives one canonical event
identity, fetches public pages, and may only append fields backed by quoted text
from those pages. It never invents a second event or writes canon directly.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup
from pydantic import BaseModel, ConfigDict

from eventindex import config, llm
from eventindex.extract.llm_text import html_to_text

MAX_PAGES = 5
MAX_PAGE_CHARS = 10_000
MAX_TOTAL_CHARS = 28_000
FACT_CONFIDENCE_CAP = 0.85


@dataclass(frozen=True)
class PublicPage:
    url: str
    text: str


class RecoveredFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    same_event: bool
    price_min: float | None
    price_max: float | None
    price_evidence: str | None
    price_source: int | None
    venue_name: str | None
    venue_evidence: str | None
    venue_source: int | None
    booking_url: str | None
    booking_evidence: str | None
    booking_source: int | None
    start_time: str | None
    start_time_evidence: str | None
    start_time_source: int | None
    confidence: float


def _readable_text(content: bytes, content_type: str) -> str:
    from eventindex.extract.pdf import is_pdf, to_text

    if is_pdf(content, content_type):
        return to_text(content)
    text = html_to_text(content)
    soup = BeautifulSoup(content, "html.parser")
    links = [
        href for tag in soup.find_all("a")
        if (href := tag.get("href")) and href.startswith(("http://", "https://"))
    ]
    return " ".join([text, *links])


def fetch_pages(urls: list[str]) -> list[PublicPage]:
    """Fetch a bounded set of public pages with the system politeness policy."""
    pages: list[PublicPage] = []
    seen: set[str] = set()
    with httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": config.USER_AGENT},
    ) as client:
        for url in urls:
            if len(pages) >= MAX_PAGES or url in seen:
                break
            seen.add(url)
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                continue
            time.sleep(config.CRAWL_DELAY_S)
            try:
                response = client.get(url)
                if response.status_code in {401, 403}:
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                text = _readable_text(
                    response.content, content_type
                )
            except (httpx.HTTPError, ValueError):
                continue
            if len(text) < 100:
                from eventindex.extract.pdf import is_pdf

                if is_pdf(response.content, content_type):
                    continue
                # A public JS shell is still human-readable after rendering.
                from eventindex.fetch.headless import render_page

                rendered = render_page(str(response.url))
                text = html_to_text(rendered) if rendered else text
            if len(text) >= 100:
                pages.append(PublicPage(str(response.url), text))
    return pages


def _evidence_on_page(
    pages: list[PublicPage], evidence: str | None, source: int | None
) -> bool:
    return (
        evidence is not None
        and source is not None
        and 0 <= source < len(pages)
        and " ".join(evidence.split()).casefold()
        in " ".join(pages[source].text.split()).casefold()
    )


def extract_facts(
    tx,
    event: dict,
    pages: list[PublicPage],
    *,
    job_id=None,
) -> tuple[dict, str | None]:
    """Return a claim-field payload plus its minimal quoted raw evidence."""
    if not pages:
        return {}, None
    rendered = []
    total = 0
    for index, page in enumerate(pages):
        chunk = page.text[:MAX_PAGE_CHARS]
        if total + len(chunk) > MAX_TOTAL_CHARS:
            chunk = chunk[: max(0, MAX_TOTAL_CHARS - total)]
        if not chunk:
            break
        rendered.append(f"[SOURCE {index}] {page.url}\n{chunk}")
        total += len(chunk)
    local_zone = ZoneInfo(config.TIMEZONE)
    start = event["starts_at"].astimezone(local_zone).isoformat()
    result = llm.complete(
        tx,
        "Recover exact public facts for ONE already-known event. The pages may "
        "contain navigation, other dates, or similarly named events. Set "
        "same_event=true only when the title/date/venue context identifies the "
        "same event. Extract only facts explicitly stated on the supplied "
        "pages; never estimate. Every returned fact needs a short VERBATIM "
        "evidence substring and the zero-based SOURCE number containing it. "
        "For a single price set min=max; free entry is 0. price is admission/"
        "ticket price, never prize money, donations, discounts without a base "
        "price, percentages, years, or unrelated offers. start_time is HH:MM "
        "local and only the event/program start, never doors or box office. "
        "booking_url must be a literal public URL shown in a source. Return "
        "null for every unsupported fact.\n\n"
        f"KNOWN EVENT\nTITLE: {event['title']}\n"
        f"DATE/TIME CURRENTLY INDEXED: {start}\n"
        f"VENUE: {event.get('venue_name')}\n"
        f"ORGANIZER: {event.get('organizer')}\n\n"
        + "\n\n".join(rendered),
        RecoveredFacts,
        source_id=event.get("source_id"),
        job_id=job_id,
    )
    if not result.same_event:
        return {}, None
    confidence = min(max(float(result.confidence), 0.0), FACT_CONFIDENCE_CAP)
    payload: dict = {}
    evidence_parts: list[str] = []
    source_indexes: list[int] = []

    if (
        result.price_min is not None
        and result.price_max is not None
        and 0 <= result.price_min <= result.price_max <= 5000
        and _evidence_on_page(
            pages, result.price_evidence, result.price_source
        )
    ):
        payload["price_min"] = {
            "value": result.price_min, "confidence": confidence
        }
        payload["price_max"] = {
            "value": result.price_max, "confidence": confidence
        }
        evidence_parts.append(result.price_evidence)
        source_indexes.append(result.price_source)

    if (
        result.venue_name
        and _evidence_on_page(
            pages, result.venue_evidence, result.venue_source
        )
    ):
        payload["venue_name"] = {
            "value": result.venue_name.strip()[:160],
            "confidence": confidence,
        }
        evidence_parts.append(result.venue_evidence)
        source_indexes.append(result.venue_source)

    if (
        result.booking_url
        and result.booking_url.startswith(("http://", "https://"))
        and result.booking_source is not None
        and 0 <= result.booking_source < len(pages)
        and (
            result.booking_url in pages[result.booking_source].text
            or result.booking_url == pages[result.booking_source].url
        )
        and _evidence_on_page(
            pages, result.booking_evidence, result.booking_source
        )
    ):
        payload["booking_url"] = {
            "value": result.booking_url, "confidence": confidence
        }
        evidence_parts.append(result.booking_evidence)
        source_indexes.append(result.booking_source)

    if (
        result.start_time
        and re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", result.start_time)
        and _evidence_on_page(
            pages, result.start_time_evidence, result.start_time_source
        )
    ):
        local = event["starts_at"].astimezone(local_zone)
        hour, minute = (int(part) for part in result.start_time.split(":"))
        payload["starts_at"] = {
            "value": local.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            ).isoformat(),
            "confidence": confidence,
        }
        evidence_parts.append(result.start_time_evidence)
        source_indexes.append(result.start_time_source)

    source_index = source_indexes[0] if source_indexes else None
    if source_index is not None:
        payload["url"] = {
            "value": pages[source_index].url, "confidence": confidence
        }
    raw = " | ".join(dict.fromkeys(
        part for part in evidence_parts if part
    ))[:2000] or None
    return payload, raw
