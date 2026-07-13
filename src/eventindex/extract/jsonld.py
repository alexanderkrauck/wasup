"""Tier a: schema.org/Event from JSON-LD script tags."""

import json

from bs4 import BeautifulSoup

CONFIDENCE = 0.95  # §7: JSON-LD fields

EVENT_TYPES = {
    "Event", "MusicEvent", "TheaterEvent", "DanceEvent", "ComedyEvent",
    "Festival", "ExhibitionEvent", "ScreeningEvent", "SportsEvent",
    "EducationEvent", "SocialEvent", "ChildrensEvent", "LiteraryEvent",
    "BusinessEvent", "FoodEvent", "VisualArtsEvent", "CourseInstance",
}


def _walk(node, found: list) -> None:
    """Collect every dict whose @type is an Event subtype, at any depth."""
    if isinstance(node, dict):
        node_type = node.get("@type", "")
        types = node_type if isinstance(node_type, list) else [node_type]
        if any(t in EVENT_TYPES for t in types):
            found.append(node)
        for value in node.values():
            _walk(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk(item, found)


def _text(value):
    """schema.org values may be strings, dicts with 'name', or lists."""
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("name") or value.get("@id")
    return value if isinstance(value, str) and value.strip() else None


def _location(node: dict) -> dict:
    out = {}
    loc = node.get("location")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        if isinstance(loc, str) and loc.strip():
            out["venue_name"] = loc
        return out
    if name := _text(loc.get("name")):
        out["venue_name"] = name
    address = loc.get("address")
    if isinstance(address, dict):
        parts = [
            address.get("streetAddress"), address.get("postalCode"),
            address.get("addressLocality"),
        ]
        address = " ".join(p for p in parts if p)
    if isinstance(address, str) and address.strip():
        out["address"] = address
    geo = loc.get("geo")
    if isinstance(geo, dict):
        try:
            out["lat"] = float(geo["latitude"])
            out["lon"] = float(geo["longitude"])
        except (KeyError, TypeError, ValueError):
            pass
    return out


def _prices(node: dict) -> dict:
    offers = node.get("offers")
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return {}
    out = {}
    low = offers.get("lowPrice", offers.get("price"))
    high = offers.get("highPrice", offers.get("price"))
    try:
        if low is not None:
            out["price_min"] = float(low)
        if high is not None:
            out["price_max"] = float(high)
    except (TypeError, ValueError):
        pass
    if isinstance(offers.get("url"), str) and offers["url"].strip():
        out["booking_url"] = offers["url"].strip()
    return out


def _to_payload(node: dict) -> dict | None:
    from eventindex.extract import field

    title = _text(node.get("name"))
    starts = node.get("startDate")
    if not title or not isinstance(starts, str):
        return None
    fields = {"title": title, "starts_at": starts}
    if isinstance(node.get("endDate"), str):
        fields["ends_at"] = node["endDate"]
    for key, src in (("description", "description"), ("url", "url"), ("image_url", "image")):
        if value := _text(node.get(src)):
            fields[key] = value
    if organizer := _text(node.get("organizer")):
        fields["organizer"] = organizer
    fields.update(_location(node))
    fields.update(_prices(node))
    return {k: field(v, CONFIDENCE) for k, v in fields.items()}


def parse(content: bytes, base_url: str = "") -> list[dict]:
    from urllib.parse import urljoin

    soup = BeautifulSoup(content, "html.parser")
    nodes: list[dict] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        _walk(data, nodes)
    payloads = [p for node in nodes if (p := _to_payload(node)) is not None]
    if base_url:
        # schema.org url/image values are frequently relative
        for p in payloads:
            for key in ("url", "image_url"):
                if key in p:
                    p[key]["value"] = urljoin(base_url, p[key]["value"])
    return payloads
