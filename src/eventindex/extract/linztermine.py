"""Tier b: linztermine.at open-data XML (§3.3 "platform API payload").

The city portal's export: per event, title/description/organizer/location/tags
plus explicit <date dFrom dTo> occurrences. One claim per date within the
expansion horizon (recurrence modelling proper is phase 2).

Quirk: the feed declares ISO-8859-1 but ships UTF-8 bytes.
"""

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from eventindex import config

VIENNA = ZoneInfo(config.TIMEZONE)

CONFIDENCE = 0.95
HORIZON_DAYS = 60
MAX_DESCRIPTION = 2000

_TAG_CATEGORY = {
    "musik": "music", "konzert": "music", "bühne": "theatre", "theater": "theatre",
    "kabarett": "theatre", "ausstellung": "art", "kunst": "art", "museen": "art",
    "märkte": "market", "messen": "market", "kinder": "family", "familie": "family",
    "sport": "sport", "film": "film", "kino": "film", "literatur": "culture",
    "wissenschaft": "learning", "bildung": "learning", "vortrag": "learning",
    "workshop": "learning", "kulinarik": "food_drink", "party": "nightlife",
    "clubbing": "nightlife", "religion": "religion", "kirche": "religion",
    "fest": "community", "feste": "community",
}


def _category(tags: list[str]) -> str | None:
    for tag in tags:
        for keyword, cat in _TAG_CATEGORY.items():
            if keyword in tag.lower():
                return cat
    return None


def _clean(text: str | None) -> str | None:
    if not text:
        return None
    text = html.unescape(html.unescape(text))
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text[:MAX_DESCRIPTION] or None


def parse(content: bytes, now: datetime | None = None) -> list[dict]:
    from eventindex.extract import field

    text = content.decode("utf-8", errors="replace")
    text = re.sub(r"^<\?xml[^>]*\?>", "", text)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    now = now or datetime.now(VIENNA)
    horizon = now + timedelta(days=HORIZON_DAYS)
    payloads = []
    for event in root.iter("event"):
        title = _clean(event.findtext("title"))
        if not title:
            continue
        fields = {"title": title}
        if description := _clean(event.findtext("description")):
            fields["description"] = description
        if location := _clean(event.findtext("location")):
            fields["venue_name"] = location
        if organizer := _clean(event.findtext("organizer")):
            fields["organizer"] = organizer
        for link in event.iter("link"):
            url = (link.findtext("url") or "").strip()
            if "linztermine.at/event" in url:
                fields["url"] = url
                break
        if event.get("freeofcharge") == "1":
            fields["price_min"] = 0.0
            fields["price_max"] = 0.0
        tags = [t.text or "" for t in event.iter("tag")]
        if category := _category(tags):
            fields["category"] = category

        for date in event.iter("date"):
            try:
                starts = datetime.strptime(
                    date.get("dFrom", ""), "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=VIENNA)
            except ValueError:
                continue
            if not (now - timedelta(days=1) <= starts <= horizon):
                continue
            occ_fields = dict(fields)
            occ_fields["starts_at"] = starts.isoformat()
            if d_to := date.get("dTo"):
                try:
                    ends = datetime.strptime(d_to, "%Y-%m-%d %H:%M:%S").replace(
                        tzinfo=VIENNA
                    )
                    occ_fields["ends_at"] = ends.isoformat()
                except ValueError:
                    pass
            payloads.append({k: field(v, CONFIDENCE) for k, v in occ_fields.items()})
    return payloads
