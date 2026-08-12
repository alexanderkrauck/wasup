"""Tier b: ICS feeds.

Each VEVENT becomes one claim.  Structured recurrence and cancellation
signals stay attached to that claim so the deterministic resolver can expand
or suppress it later.
"""

from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

import icalendar

from eventindex import config

CONFIDENCE = 0.95

_CANCELLED_TITLE_MARKERS = {"ABGESAGT", "CANCELLED", "CANCELED"}
_MARKER_PUNCTUATION = "!?.,:;()[]{}-–—*_\"'„“”"
VIENNA = ZoneInfo(config.TIMEZONE)


def _title_and_status(title: str) -> tuple[str, bool]:
    """Strip an explicit editorial cancellation label from an ICS title.

    Some calendar providers (notably Teamup) do not emit RFC ``STATUS`` when
    an organizer adds ``ABGESAGT``.  Requiring an uppercase standalone marker
    and rejecting question-shaped titles keeps this a mechanical label path,
    while covering both ``ABGESAGT Event`` and Teamup's ``Event ABGESAGT
    (Organizer)`` form.  Removing it also preserves the original fingerprint.
    """
    tokens = title.split()
    marker_indexes = [
        index for index, token in enumerate(tokens)
        if token.strip(_MARKER_PUNCTUATION) in _CANCELLED_TITLE_MARKERS
    ]
    if not marker_indexes or "?" in title:
        return title, False
    kept = [
        token for index, token in enumerate(tokens)
        if index not in marker_indexes and token.strip(_MARKER_PUNCTUATION)
    ]
    cleaned = " ".join(kept).strip(" !.,:;-–—*_\"'„“”")
    return (cleaned or title), True


def _vienna_datetime(value: date | datetime) -> datetime:
    """Canonicalize RFC floating/date values to this index's local timezone."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=VIENNA)
        return value.astimezone(VIENNA)
    return datetime.combine(value, time.min, tzinfo=VIENNA)


def _recurrence_value(value) -> date | datetime | None:
    """Return a dateutil-compatible start, including RDATE PERIOD starts."""
    if isinstance(value, tuple):
        value = value[0]
    return value if isinstance(value, (date, datetime)) else None


def _recurrence_set(vevent) -> str | None:
    """Serialize the complete RFC recurrence set for ``dateutil``.

    An RRULE without its EXDATEs is actively wrong, while RDATE can be a
    recurrence set on its own.  Canonicalizing DTSTART to Europe/Vienna and
    date lists to UTC keeps all-day, floating and foreign/custom TZID feeds
    aware and parseable by dateutil.  RDATE periods retain their starts; their
    per-instance duration cannot be represented by the existing occurrence
    contract and therefore falls back to the master duration.
    """
    recurrence_properties = [
        (name, value)
        for name, value in vevent.property_items(recursive=False, sorted=False)
        if name in {"RRULE", "RDATE", "EXDATE"}
    ]
    if not any(name in {"RRULE", "RDATE"} for name, _ in recurrence_properties):
        return None
    dtstart = vevent.get("DTSTART")
    if dtstart is None:
        return None
    local_start = _vienna_datetime(dtstart.dt)
    lines = [
        f"DTSTART;TZID={config.TIMEZONE}:"
        f"{local_start.strftime('%Y%m%dT%H%M%S')}"
    ]
    for name, value in recurrence_properties:
        if name == "RRULE":
            lines.append(str(vevent.content_line(name, value)))
            continue
        # dateutil rejects TZID on RDATE and cannot mix aware rules with
        # floating/date exceptions. One aware UTC form handles all three.
        values = [
            parsed for item in value.dts
            if (parsed := _recurrence_value(item.dt)) is not None
        ]
        if values:
            encoded = [
                _vienna_datetime(item).astimezone(timezone.utc).strftime(
                    "%Y%m%dT%H%M%SZ"
                )
                for item in values
            ]
            lines.append(f"{name}:{','.join(encoded)}")
    return "\n".join(lines)


def parse(content: bytes) -> list[dict]:
    from eventindex.extract import field

    try:
        cal = icalendar.Calendar.from_ical(content)
    except ValueError:
        return []
    payloads = []
    for vevent in cal.walk("VEVENT"):
        raw_title = str(vevent.get("SUMMARY", "")).strip()
        title, title_cancelled = _title_and_status(raw_title)
        calendar_uid = str(vevent.get("UID", "")).strip()
        recurrence_id = vevent.get("RECURRENCE-ID")
        dtstart = vevent.get("DTSTART") or recurrence_id
        if not title or dtstart is None:
            continue
        status = str(vevent.get("STATUS", "")).strip().upper()
        cancelled = title_cancelled or status in {"CANCELLED", "CANCELED"}
        # A cancelled exception identifies the original beat with
        # RECURRENCE-ID; its optional DTSTART may name a replacement time.
        starts_value = recurrence_id.dt if cancelled and recurrence_id else dtstart.dt
        fields = {"title": title, "starts_at": starts_value.isoformat()}
        if calendar_uid:
            fields["calendar_uid"] = calendar_uid
        if cancelled:
            fields["status"] = "cancelled"
        if recurrence_id is not None:
            fields["recurrence_id"] = recurrence_id.dt.isoformat()
            recurrence_range = str(
                recurrence_id.params.get("RANGE", "")
            ).strip().upper()
            if recurrence_range == "THISANDFUTURE":
                fields["recurrence_range"] = "this_and_future"
        if (dtend := vevent.get("DTEND")) is not None:
            fields["ends_at"] = dtend.dt.isoformat()
        if location := str(vevent.get("LOCATION", "")).strip():
            fields["venue_name"] = location
        if description := str(vevent.get("DESCRIPTION", "")).strip():
            fields["description"] = description
        if url := str(vevent.get("URL", "")).strip():
            fields["url"] = url
        if recurrence_set := _recurrence_set(vevent):
            fields["rrule_raw"] = recurrence_set
        payloads.append({k: field(v, CONFIDENCE) for k, v in fields.items()})
    return payloads
