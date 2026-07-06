"""The probe (H4): the single chokepoint every discovered candidate passes.

fetch -> "does this domain emit Linz-area events?" (mini model, few-shot)
-> score > 0.8 auto-register + onboard; 0.5-0.8 review queue; below: drop.
Junk that slips through dies economically via yield_ema decay (H4.2).
"""

import logging
import time
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, ConfigDict, Field

from eventindex import config, llm
from eventindex.extract.llm_text import html_to_text

log = logging.getLogger("eventindex.probe")

AUTO_REGISTER = 0.8
REVIEW = 0.5


class ProbeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emits_events: bool = Field(description="site announces events/courses/Termine with dates")
    linz_area: bool = Field(description="located in/serving Linz metro (~25km: Leonding, Traun, Ansfelden, Enns, ...)")
    score: float = Field(description="0-1: probability this is a worthwhile Linz event source")
    suggested_name: str
    listing_url: str | None = Field(description="best URL of the actual event/course listing page, if visible")
    entity_type: str | None = Field(description="venue|gym|verein|church|university|promoter|portal|other")


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def known_domains(tx) -> set[str]:
    return {domain_of(r["url"]) for r in tx.execute("SELECT url FROM source")}


def probe_url(tx, url: str, discovered_via: str, job_id=None) -> dict:
    """Returns {"outcome": registered|review|rejected|error, ...}."""
    if domain_of(url) in known_domains(tx):
        return {"outcome": "known"}
    time.sleep(config.CRAWL_DELAY_S)
    try:
        resp = httpx.get(url, headers={"User-Agent": config.USER_AGENT},
                         timeout=20, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        return {"outcome": "error", "detail": str(e)[:200]}

    text = html_to_text(resp.content)[:6000]
    if len(text) < 80:
        return {"outcome": "rejected", "detail": "empty/JS-shell page"}

    verdict = llm.complete(
        tx,
        f"Candidate source for a Linz (Austria) event index.\nURL: {resp.url}\n\n"
        f"PAGE TEXT:\n{text}\n\n"
        "Judge: does this website emit events/courses/regular meetings in the "
        "Linz metro area (~25km)? Examples of YES: a gym timetable in Leonding, "
        "a Verein with 'Termine', a Pfarre with Feste, a club with a program. "
        "Examples of NO: a Vienna venue, a webshop, a company site without "
        "events, a news site without dated listings.",
        ProbeVerdict,
        job_id=job_id,
    )
    score = min(max(verdict.score, 0.0), 1.0)
    if not (verdict.emits_events and verdict.linz_area):
        score = min(score, 0.4)

    if score > AUTO_REGISTER:
        source_url = verdict.listing_url or str(resp.url)
        row = tx.execute(
            """
            INSERT INTO source (name, url, kind, entity_type, tier, trust,
                                monthly_budget_eur, discovered_via)
            VALUES (%s, %s, 'website', %s, 3, 0.65, %s, %s)
            ON CONFLICT (url) DO NOTHING RETURNING id
            """,
            (verdict.suggested_name[:120], source_url, verdict.entity_type,
             config.MONTHLY_BUDGET_EUR_BY_TIER[3], discovered_via),
        ).fetchone()
        if row is None:
            return {"outcome": "known"}
        return {"outcome": "registered", "source_id": row["id"], "score": score}
    if score >= REVIEW:
        _review(url, score, verdict)
        return {"outcome": "review", "score": score}
    return {"outcome": "rejected", "score": score, "detail": verdict.suggested_name}


def _review(url: str, score: float, verdict: ProbeVerdict) -> None:
    review_dir = config.VAR_DIR / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    path = review_dir / f"probes-{now:%Y-%m-%d}.md"
    with path.open("a") as f:
        f.write(f"- [ ] {score:.2f} {verdict.suggested_name} — {url} "
                f"(listing: {verdict.listing_url or '?'})\n")
