"""Tier b: RSS/Atom. Only items carrying a structured *event* date (xCal /
ev: namespaces) parse for free - pubDate is publication time, not event time.
Feeds without event dates fall through to the LLM tier on the feed's text.
"""

import feedparser

CONFIDENCE = 0.9

# feedparser lowercases and joins namespace keys: ev:startdate -> ev_startdate
_START_KEYS = ("ev_startdate", "xcal_dtstart", "start")
_END_KEYS = ("ev_enddate", "xcal_dtend", "end")


def _first(entry, keys):
    for key in keys:
        if value := entry.get(key):
            return value
    return None


def parse(content: bytes) -> list[dict]:
    from eventindex.extract import field

    feed = feedparser.parse(content)
    payloads = []
    for entry in feed.entries:
        title = (entry.get("title") or "").strip()
        starts = _first(entry, _START_KEYS)
        if not title or not starts:
            continue
        fields = {"title": title, "starts_at": starts}
        if ends := _first(entry, _END_KEYS):
            fields["ends_at"] = ends
        if link := entry.get("link"):
            fields["url"] = link
        if summary := entry.get("summary"):
            fields["description"] = summary
        if location := entry.get("xcal_location") or entry.get("ev_location"):
            fields["venue_name"] = location
        payloads.append({k: field(v, CONFIDENCE) for k, v in fields.items()})
    return payloads


def to_text(content: bytes) -> str:
    """Flatten feed items to text for the LLM tier."""
    feed = feedparser.parse(content)
    chunks = []
    for entry in feed.entries:
        parts = [entry.get("title", ""), entry.get("summary", ""), entry.get("link", "")]
        chunks.append("\n".join(p for p in parts if p))
    return "\n\n---\n\n".join(chunks)
