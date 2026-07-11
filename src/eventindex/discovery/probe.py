"""The probe (H4): the single chokepoint every discovered candidate passes.

fetch -> "does this domain emit Linz-area events?" (mini model) -> score >=
0.5 registers ALWAYS, with any doubts recorded as probe_concerns attributes
(Alexander, 2026-07-06: when in doubt, crawl - a gym whose courses need
membership is an attribute, not an exclusion). Below 0.5: drop. Junk that
slips through dies economically via yield_ema decay (H4.2).
"""

import logging
import time
from urllib.parse import urlparse

import httpx
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field

from eventindex import config, llm
from eventindex.extract.llm_text import html_to_text

log = logging.getLogger("eventindex.probe")

REGISTER = 0.5


class ProbeVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emits_events: bool = Field(description="site announces events/courses/Termine with dates")
    linz_area: bool = Field(description="located in/serving Linz metro (~25km: Leonding, Traun, Ansfelden, Enns, ...)")
    score: float = Field(description="0-1: probability this is a worthwhile Linz event source")
    suggested_name: str
    listing_url: str | None = Field(description="best URL of the actual event/course listing page, if visible")
    entity_type: str | None = Field(description="venue|gym|verein|church|university|promoter|portal|other")
    concerns: list[str] = Field(
        description="doubts that lower the score, as short slugs, e.g. "
        "membership_required, paid_courses_only, region_unclear, "
        "low_event_signal, mostly_past_events, commercial_venue_no_program"
    )


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def is_owned_by(host: str, probed: str) -> bool:
    """True when host is the probed domain itself or one of its own
    subdomains (events.x.at under x.at). This is the only kind of
    listing_url a probe may adopt: page text is untrusted input, anything
    off the probed site would register an arbitrary third party. Distinct
    tenants on shared platforms (a.jimdo.com vs b.jimdo.com) stay distinct
    - a sibling subdomain is never "owned"."""
    return host == probed or host.endswith("." + probed)


def is_known(domain: str, known: set[str]) -> bool:
    """A candidate domain is known if a source exists on that host OR on
    any subdomain of it: a source registered at events.x.at must make the
    x.at apex known, or every future sweep re-probes it forever (no
    convergence)."""
    return domain in known or any(k.endswith("." + domain) for k in known)


def known_domains(tx) -> set[str]:
    return {domain_of(r["url"]) for r in tx.execute("SELECT url FROM source")}


def probe_url(tx, url: str, discovered_via: str, job_id=None) -> dict:
    """Returns {"outcome": registered|known|rejected|error, ...}."""
    if is_known(domain_of(url), known_domains(tx)):
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

    if score < REGISTER:
        return {"outcome": "rejected", "score": score, "detail": verdict.suggested_name}

    # the model may suggest a deeper listing URL, but only on the probed
    # site itself or its own subdomains (events.factory300.at under
    # factory300.at - the exact-host rule cost us that listing and the
    # onboard exhausted from the bare homepage, found live 2026-07-11);
    # is_known() keeps the apex known afterwards, so sweeps still converge
    source_url = str(resp.url)
    if verdict.listing_url and is_owned_by(
        domain_of(verdict.listing_url), domain_of(source_url)
    ):
        source_url = verdict.listing_url
    row = tx.execute(
        """
        INSERT INTO source (name, url, kind, entity_type, tier, trust,
                            monthly_budget_eur, discovered_via, extraction_hint)
        VALUES (%s, %s, 'website', %s, 3, 0.65, %s, %s, %s)
        ON CONFLICT (url) DO NOTHING RETURNING id
        """,
        (verdict.suggested_name[:120], source_url, verdict.entity_type,
         config.MONTHLY_BUDGET_EUR_BY_TIER[3], discovered_via,
         Jsonb({"probe_score": score, "probe_concerns": verdict.concerns})),
    ).fetchone()
    if row is None:
        return {"outcome": "known"}
    return {"outcome": "registered", "source_id": row["id"], "score": score,
            "detail": ",".join(verdict.concerns)[:120]}
