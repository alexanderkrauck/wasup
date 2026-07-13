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
from eventindex.discovery.probe import (
    domain_of, is_known, known_domains, recently_rejected_domains,
)

log = logging.getLogger("eventindex.sweep")

PLACES_CATEGORIES = [
    "Nachtclub", "Konzertlokal", "Fitnessstudio", "Kletterhalle", "Tanzschule",
    "Museum", "Galerie", "Theater", "Jugendzentrum", "Pfarre", "Sportverein",
    "Kulturverein", "Bibliothek", "Brettspiel Verein", "Volkshaus",
    "Coworking Space", "Veranstaltungszentrum", "Buchhandlung", "Kino",
    "Kabarett", "Kongresszentrum", "Brauerei",
]
PLACES_COST_EUR = 0.03  # per text-search request, logged for governance
MAX_PROBES_PER_SWEEP = 120

OVERPASS_QUERY = """
[out:json][timeout:60];
(
  nwr["amenity"~"community_centre|theatre|arts_centre|social_centre|events_venue|conference_centre|cinema|nightclub"]({bbox});
  nwr["leisure"~"sports_centre|fitness_centre|climbing|dance|bowling_alley|ice_rink"]({bbox});
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
    # 2026-07-10: the original list was pure leisure vocabulary - the whole
    # business/institutional world was unfindable by construction (caught
    # live when a consumer asked for startup/pitch events: tech2b, Startup
    # Live, WKO were all absent). Below: a systematic pass over every
    # event-emitting segment, not just the holes we tripped over.
    "startup pitch event", "gründer treffen netzwerken", "tech meetup",
    "vortrag diskussion wissenschaft", "poetry slam lesung",
    "english speaking events expat", "senioren veranstaltungen programm",
    "esports gaming turnier", "ehrenamt freiwilligenarbeit",
    "open mic jam session", "kulinarik festival verkostung",
    "freikirche moschee buddhistisch veranstaltungen",
    "lgbtq queer veranstaltungen", "kinder ferienprogramm workshop",
    "karrieremesse jobmesse", "bauernmarkt wochenmarkt",
    "musikverein blasmusik konzert", "kampfsport training probetraining",
    "radtreff ausfahrt", "pen and paper rollenspiel",
    "meditation achtsamkeit kurs", "podiumsdiskussion politik",
    "oldtimer treffen", "makerspace werkstatt kurs",
    "festival sommerfest", "tanzabend ball",
]
SEARCH_AREAS = ["Linz", "Linz Urfahr", "Leonding", "Traun",
                "Enns", "Ansfelden", "Ottensheim", "Pasching"]
MAX_SEARCHES_PER_SWEEP = 80  # matrix rotates; ~5 weekly sweeps cover it all
_PORTAL_NOISE = (
    "linztermine", "facebook.", "instagram.", "eventbrite", "meetup.com",
    "tiktok.", "youtube.", "google.", "tripadvisor", "yelp.", "linkedin.",
    "wikipedia.", "herold.at", "firmenabc", "willhaben", "strava.com",
    "tips.at", "meinbezirk.at", "nachrichten.at", "1000things",
)


def search_web(tx, query: str, job_id=None) -> list[str]:
    """One web search via the OpenRouter web plugin (Exa engine): result
    URLs arrive as url_citation annotations - machine-readable, budget-
    ledgered through the one LLM client, no extra account or key.
    (Google CSE: whole-web deprecated for new engines, dead Jan 2027.
    Gemini grounding: ToS forbids using links for crawling. Brave: vetoed.)"""
    from eventindex import llm

    msg = llm.chat(
        tx,
        [{"role": "user", "content": f"Suche im Web: {query}"}],
        model=config.MODEL_MINI,
        job_id=job_id,
        plugins=[{"id": "web", "engine": "exa", "max_results": 10}],
    )
    annotations = getattr(msg, "annotations", None) or []
    urls = []
    for a in annotations:
        a = a if isinstance(a, dict) else a.model_dump()
        if a.get("type") == "url_citation":
            if url := a.get("url_citation", {}).get("url"):
                urls.append(url)
    return urls


def sweep_search(tx, job_id=None) -> list[str]:
    """§4d fan-out over the query matrix, rotating across sweeps so the
    monthly cadence covers all combinations."""
    matrix = [f"{term} {area}" for term in SEARCH_TERMS for area in SEARCH_AREAS]
    offset_row = tx.execute(
        "SELECT count(*) AS n FROM crawl_log WHERE detail LIKE 'discover[search]%'"
    ).fetchone()
    start = (offset_row["n"] * MAX_SEARCHES_PER_SWEEP) % len(matrix)
    queries = (matrix + matrix)[start : start + MAX_SEARCHES_PER_SWEEP]

    urls: list[str] = []
    for query in queries:
        try:
            hits = search_web(tx, query, job_id=job_id)
        except Exception as e:
            log.warning("search %r failed: %s", query, e)
            continue
        urls += [u for u in hits if not any(n in u for n in _PORTAL_NOISE)]
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
    rejected = recently_rejected_domains(tx)
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
        if not domain or is_known(domain, known) or domain in queued_domains \
                or domain in already_queued or domain in rejected:
            continue
        if enqueued >= MAX_PROBES_PER_SWEEP:
            break
        enqueue(tx, "probe", {"url": url, "discovered_via": channel})
        queued_domains.add(domain)
        enqueued += 1
    return len(urls), enqueued
