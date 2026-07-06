"""Discovery sweeps (§4): each channel yields candidate URLs -> probe jobs.

Channels: Google Places text search (a), portal backlink mining (b),
OSM Overpass (a). Search fan-out (d) is parked - needs a search API
(OPEN-QUESTIONS). All bounded; the probe is the quality chokepoint.
"""

import logging
import re
import time

import httpx

from eventindex import config
from eventindex.budget import record_spend
from eventindex.discovery.probe import domain_of, known_domains

log = logging.getLogger("eventindex.sweep")

PLACES_CATEGORIES = [
    "Nachtclub", "Konzertlokal", "Fitnessstudio", "Kletterhalle", "Tanzschule",
    "Museum", "Galerie", "Theater", "Jugendzentrum", "Pfarre", "Sportverein",
    "Kulturverein", "Bibliothek", "Brettspiel Verein", "Volkshaus",
]
PLACES_COST_EUR = 0.03  # per text-search request, logged for governance
MAX_PROBES_PER_SWEEP = 120

OVERPASS_QUERY = """
[out:json][timeout:60];
(
  nwr["amenity"~"community_centre|theatre|arts_centre|social_centre"]({bbox});
  nwr["leisure"~"sports_centre|fitness_centre|climbing|dance"]({bbox});
  nwr["club"]({bbox});
)->.all;
nwr.all[~"^(website|contact:website)$"~"."]({bbox});
out tags;
"""
BBOX = "48.08,13.95,48.53,14.63"  # Linz +/- ~25km


def sweep_google_places(tx, job_id=None) -> list[str]:
    if not config.GOOGLE_PLACES_API_KEY:
        raise RuntimeError("GOOGLE_PLACES_API_KEY not set")
    urls: list[str] = []
    for category in PLACES_CATEGORIES:
        resp = httpx.post(
            "https://places.googleapis.com/v1/places:searchText",
            headers={
                "X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY,
                "X-Goog-FieldMask": "places.displayName,places.websiteUri",
            },
            json={
                "textQuery": f"{category} Linz Österreich",
                "maxResultCount": 20,
                "locationBias": {"circle": {
                    "center": {"latitude": 48.3069, "longitude": 14.2858},
                    "radius": 25000.0,
                }},
            },
            timeout=30,
        )
        record_spend(PLACES_COST_EUR, "other", job_id=job_id,
                     detail=f"places textsearch '{category}'")
        if resp.status_code != 200:
            log.warning("places search %s -> %s %s", category, resp.status_code,
                        resp.text[:200])
            continue
        for place in resp.json().get("places", []):
            if website := place.get("websiteUri"):
                urls.append(website)
        time.sleep(0.3)
    return urls


def sweep_osm(tx, job_id=None) -> list[str]:
    resp = httpx.post(
        "https://overpass-api.de/api/interpreter",
        data={"data": OVERPASS_QUERY.replace("{bbox}", BBOX)},
        headers={"User-Agent": config.USER_AGENT},
        timeout=90,
    )
    resp.raise_for_status()
    urls = []
    for el in resp.json().get("elements", []):
        tags = el.get("tags", {})
        if website := (tags.get("website") or tags.get("contact:website")):
            urls.append(website)
    return urls


def sweep_backlinks(tx, job_id=None) -> list[str]:
    """§4b: mine organizer/info links out of portal claims - the portal shows
    10% of what the organizer's own site has."""
    rows = tx.execute(
        """
        SELECT DISTINCT p.url FROM (
            SELECT payload->'url'->>'value' AS url FROM event_claim
            UNION SELECT payload->'organizer'->>'value' FROM event_claim
            UNION SELECT payload->'description'->>'value' FROM event_claim
        ) p WHERE p.url IS NOT NULL
        """
    ).fetchall()
    urls = set()
    for r in rows:
        for m in re.finditer(r"https?://[^\s\"'<>)\]]+", r["url"]):
            urls.add(m.group(0).rstrip(".,;"))
    return sorted(urls)


# §4d search fan-out: the main net for niche/placeless sources (run clubs,
# communities, meetups) that Places/OSM structurally cannot see.
SEARCH_TERMS = [
    "run club", "lauftreff", "yoga", "klettern bouldern", "salsa tanzen",
    "schachverein", "flohmarkt", "repair cafe", "brettspiele treff",
    "sprachaustausch language exchange", "sip and paint", "töpfern workshop",
    "community club", "stammtisch verein", "krafttraining kurse",
    "volleyball hobby", "wandern gruppe", "fotografie workshop",
    "kochkurs", "chor singen",
]
SEARCH_AREAS = ["Linz", "Linz Urfahr", "Leonding", "Traun"]
MAX_SEARCHES_PER_SWEEP = 40  # matrix rotates; monthly cadence covers it all
_PORTAL_NOISE = (
    "linztermine", "facebook.", "instagram.", "eventbrite", "meetup.com",
    "tiktok.", "youtube.", "google.", "tripadvisor", "yelp.", "linkedin.",
    "wikipedia.", "herold.at", "firmenabc", "willhaben",
)


def sweep_search(tx, job_id=None) -> list[str]:
    """Brave Search API over the query matrix; rotates through the matrix
    across sweeps (offset persisted in a tiny state row)."""
    if not config.BRAVE_SEARCH_API_KEY:
        raise RuntimeError("BRAVE_SEARCH_API_KEY not set (OPEN-QUESTIONS #12)")
    matrix = [f"{term} {area}" for term in SEARCH_TERMS for area in SEARCH_AREAS]
    offset_row = tx.execute(
        "SELECT count(*) AS n FROM crawl_log WHERE detail LIKE 'discover[search]%'"
    ).fetchone()
    start = (offset_row["n"] * MAX_SEARCHES_PER_SWEEP) % len(matrix)
    queries = (matrix + matrix)[start : start + MAX_SEARCHES_PER_SWEEP]

    urls: list[str] = []
    for query in queries:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": 10, "country": "at"},
            headers={"X-Subscription-Token": config.BRAVE_SEARCH_API_KEY,
                     "Accept": "application/json"},
            timeout=20,
        )
        if resp.status_code != 200:
            log.warning("brave search %r -> %s", query, resp.status_code)
            continue
        for hit in resp.json().get("web", {}).get("results", []):
            url = hit.get("url", "")
            if url and not any(noise in url for noise in _PORTAL_NOISE):
                urls.append(url)
        time.sleep(1.1)  # free-tier rate limit
    return urls


CHANNELS = {
    "google_places": sweep_google_places,
    "osm": sweep_osm,
    "backlinks": sweep_backlinks,
    "search": sweep_search,
}


def discover(tx, channel: str, job_id=None) -> tuple[int, int]:
    """Run one channel; enqueue probe jobs for unknown domains.
    Returns (candidates_seen, probes_enqueued)."""
    from eventindex.jobs.worker import enqueue

    urls = CHANNELS[channel](tx, job_id=job_id)
    known = known_domains(tx)
    queued_domains: set[str] = set()
    enqueued = 0
    already_queued = {
        domain_of(r["url"]) for r in tx.execute(
            "SELECT payload->>'url' AS url FROM jobs "
            "WHERE kind = 'probe' AND status IN ('pending', 'running')"
        ) if r["url"]
    }
    for url in urls:
        domain = domain_of(url)
        if not domain or domain in known or domain in queued_domains \
                or domain in already_queued:
            continue
        if enqueued >= MAX_PROBES_PER_SWEEP:
            break
        enqueue(tx, "probe", {"url": url, "discovered_via": channel})
        queued_domains.add(domain)
        enqueued += 1
    return len(urls), enqueued
