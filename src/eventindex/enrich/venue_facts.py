"""Generic grounding of canonical venues against public place information."""

from __future__ import annotations

from difflib import SequenceMatcher

import httpx
from pydantic import BaseModel, ConfigDict, Field

from eventindex import config, llm
from eventindex.budget import record_spend
from eventindex.enrich.facts import PublicPage, _evidence_on_page
from eventindex.resolve.fingerprint import normalize_title

PLACES_COST_EUR = 0.03
PLACE_NAME_MIN_SIMILARITY = 0.55


class VenueCapacity(BaseModel):
    model_config = ConfigDict(extra="forbid")
    same_venue: bool
    capacity: int | None = Field(ge=1, le=1_000_000)
    evidence: str | None
    source: int | None
    confidence: float = Field(ge=0, le=1)


def _name_similarity(left: str, right: str) -> float:
    return SequenceMatcher(
        None, normalize_title(left), normalize_title(right)
    ).ratio()


def find_place(venue: dict, *, job_id=None) -> dict | None:
    """Return one mechanically corroborated Google place, or no match."""
    if not config.GOOGLE_PLACES_API_KEY:
        raise RuntimeError("GOOGLE_PLACES_API_KEY not set")
    response = httpx.post(
        "https://places.googleapis.com/v1/places:searchText",
        headers={
            "X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": (
                "places.id,places.displayName,places.formattedAddress,"
                "places.location,places.websiteUri"
            ),
        },
        json={
            "textQuery": " ".join(filter(None, [
                venue["name"], venue.get("address"),
            ])),
            "maxResultCount": 5,
            "languageCode": "de",
            "regionCode": "AT",
            "locationBias": {
                "circle": {
                    "center": {"latitude": 48.3069, "longitude": 14.2858},
                    "radius": 100_000.0,
                }
            },
        },
        timeout=30,
    )
    record_spend(
        PLACES_COST_EUR, "other", job_id=job_id,
        detail=f"venue place grounding '{venue['name'][:100]}'",
    )
    response.raise_for_status()
    candidates = response.json().get("places", [])
    ranked = sorted(
        candidates,
        key=lambda place: _name_similarity(
            venue["name"], (place.get("displayName") or {}).get("text", "")
        ),
        reverse=True,
    )
    if not ranked:
        return None
    best = ranked[0]
    display_name = (best.get("displayName") or {}).get("text", "")
    best_score = _name_similarity(venue["name"], display_name)
    if best_score < PLACE_NAME_MIN_SIMILARITY:
        return None
    # Repeated generic names (several "Pfarrsaal"/"Volkshaus" candidates)
    # must remain unknown unless the existing address disambiguates them.
    # A wrong point is worse than no point because it silently excludes a
    # real event from locality searches.
    if venue.get("address") is None and len(ranked) > 1:
        runner_name = (ranked[1].get("displayName") or {}).get("text", "")
        if _name_similarity(venue["name"], runner_name) >= best_score - 0.05:
            return None
    location = best.get("location") or {}
    if location.get("latitude") is None or location.get("longitude") is None:
        return None
    return best


def extract_capacity(
    tx, venue: dict, pages: list[PublicPage], *, job_id=None
) -> tuple[int | None, str | None, str | None]:
    """Extract only a publicly stated venue capacity with verbatim evidence."""
    if not pages:
        return None, None, None
    rendered = "\n\n".join(
        f"[SOURCE {index}] {page.url}\n{page.text[:10_000]}"
        for index, page in enumerate(pages[:5])
    )[:28_000]
    result = llm.complete(
        tx,
        "Recover the explicitly stated maximum capacity for ONE known venue. "
        "The pages may describe other rooms, events, or organizations. Set "
        "same_venue=true only when the name/address identifies this venue. "
        "capacity is the total number of people/guests/seats the venue or its "
        "main event space can hold; never infer it from photos, event turnout, "
        "room size, or a list of capacities. Return null if ambiguous. A "
        "capacity needs a short VERBATIM evidence substring and the zero-based "
        "SOURCE containing it.\n\n"
        f"KNOWN VENUE: {venue['name']}\n"
        f"KNOWN ADDRESS: {venue.get('address') or '-'}\n\n{rendered}",
        VenueCapacity,
        job_id=job_id,
    )
    if (
        not result.same_venue
        or result.capacity is None
        or not _evidence_on_page(pages, result.evidence, result.source)
    ):
        return None, None, None
    source_url = pages[result.source].url
    return result.capacity, result.evidence, source_url
