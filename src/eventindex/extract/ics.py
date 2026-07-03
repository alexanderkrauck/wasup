"""Tier b: ICS feeds. Recurrence expansion is phase 2; here each VEVENT's own
DTSTART is one claim (RRULE noted in the payload for later)."""

import icalendar

CONFIDENCE = 0.95


def parse(content: bytes) -> list[dict]:
    from eventindex.extract import field

    try:
        cal = icalendar.Calendar.from_ical(content)
    except ValueError:
        return []
    payloads = []
    for vevent in cal.walk("VEVENT"):
        title = str(vevent.get("SUMMARY", "")).strip()
        dtstart = vevent.get("DTSTART")
        if not title or dtstart is None:
            continue
        fields = {"title": title, "starts_at": dtstart.dt.isoformat()}
        if (dtend := vevent.get("DTEND")) is not None:
            fields["ends_at"] = dtend.dt.isoformat()
        if location := str(vevent.get("LOCATION", "")).strip():
            fields["venue_name"] = location
        if description := str(vevent.get("DESCRIPTION", "")).strip():
            fields["description"] = description
        if url := str(vevent.get("URL", "")).strip():
            fields["url"] = url
        if (rrule := vevent.get("RRULE")) is not None:
            fields["rrule_raw"] = icalendar.vRecur(rrule).to_ical().decode()
        payloads.append({k: field(v, CONFIDENCE) for k, v in fields.items()})
    return payloads
