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
    linz_area: bool = Field(
        description="the listed events INCLUDE events in the Linz metro "
        "(~25km: Leonding, Traun, Ansfelden, Enns, ...); also true for "
        "wider-region (OÖ/state-wide) institutions and portals whose "
        "calendar contains Linz-area events among others"
    )
    score: float = Field(description="0-1: probability this is a worthwhile Linz event source")
    suggested_name: str
    listing_url: str | None = Field(
        description="best URL of the actual event/course listing page, if "
        "visible; for wider-region sources prefer the most Linz-specific "
        "view the site offers (e.g. a location/district filter)"
    )
    entity_type: str | None = Field(description="venue|gym|verein|church|university|promoter|portal|other")
    concerns: list[str] = Field(
        description="doubts that lower the score, as short slugs, e.g. "
        "membership_required, paid_courses_only, region_unclear, "
        "regional_mixed_locality, low_event_signal, mostly_past_events, "
        "commercial_venue_no_program"
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


def recently_rejected_domains(tx) -> set[str]:
    """Domains a probe rejected within the TTL. Sweeps skip these (before
    this memory, rejected domains re-entered every sweep and crowded the
    probe cap: sport-ooe.at was fetched and judged 3x in one week). Exact
    match only - a rejected apex must not block its subdomains, the good
    listing may live there. Direct probe jobs bypass this entirely."""
    return {
        r["domain"] for r in tx.execute(
            "SELECT domain FROM probe_rejection "
            "WHERE rejected_at > now() - %s * interval '1 day'",
            (config.PROBE_REJECT_TTL_DAYS,),
        )
    }


def _reject(tx, url: str, detail: str, score: float | None = None) -> dict:
    """Record the verdict (score + concerns survive for H4.1 forensics)
    and remember the domain so sweeps stop re-probing it for the TTL."""
    tx.execute(
        "INSERT INTO probe_rejection (domain, url, detail, score) "
        "VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (domain) DO UPDATE SET url = excluded.url, "
        "detail = excluded.detail, score = excluded.score, rejected_at = now()",
        (domain_of(url), url, detail, score),
    )
    out = {"outcome": "rejected", "detail": detail}
    if score is not None:
        out["score"] = score
    return out


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
        return _reject(tx, url, "empty/JS-shell page")

    # locality semantics follow H4.3 "index generously, filter at serve
    # time": a wider-region source whose calendar includes Linz events IS
    # a Linz event source - WKO OÖ, Landesbibliothek and Kultik (Treffling,
    # 8km away) were auto-rejected as "not Linz-specific" (found 2026-07-13)
    verdict = llm.complete(
        tx,
        f"Candidate source for a Linz (Austria) event index.\nURL: {resp.url}\n\n"
        f"PAGE TEXT:\n{text}\n\n"
        "Judge: does this website emit events/courses/regular meetings that "
        "take place in the Linz metro area (~25km)? Examples of YES: a gym "
        "timetable in Leonding, a Verein with 'Termine', a Pfarre with Feste, "
        "a club with a program, a state-wide (OÖ) institution or portal whose "
        "calendar includes Linz-area events among others (non-Linz entries "
        "are filtered downstream). Examples of NO: a Vienna or Munich venue, "
        "a webshop, a company site without events, a news site without dated "
        "listings, a regional site whose events all lie outside the Linz "
        "area. If unsure whether a town lies within ~25km of Linz, assume "
        "it does.",
        ProbeVerdict,
        job_id=job_id,
    )
    score = min(max(verdict.score, 0.0), 1.0)
    if not (verdict.emits_events and verdict.linz_area):
        score = min(score, 0.4)

    if score < REGISTER:
        detail = (f"{verdict.suggested_name} score={score:.2f} "
                  f"[{','.join(verdict.concerns)}]")[:300]
        return _reject(tx, url, detail, score)

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
    # a stale rejection must not keep sweeps away from a now-registered domain
    tx.execute("DELETE FROM probe_rejection WHERE domain = %s", (domain_of(url),))
    return {"outcome": "registered", "source_id": row["id"], "score": score,
            "detail": ",".join(verdict.concerns)[:120]}
