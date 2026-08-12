"""Extractor regression tests on recorded fixtures (HURDLES H3.4)."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from eventindex.extract import (
    ics, is_upcoming, jsonld, linztermine, normalize_claim, parse_dt,
)

FIXTURES = Path(__file__).parent / "fixtures"
VIENNA = ZoneInfo("Europe/Vienna")


def test_jsonld_eventbrite_fixture():
    content = (FIXTURES / "eventbrite_jsonld.html").read_bytes()
    payloads = jsonld.parse(content)
    assert len(payloads) >= 10
    for p in payloads:
        assert p["title"]["value"]
        assert p["title"]["confidence"] == 0.95
        assert parse_dt(p["starts_at"]["value"]) is not None
    # eventbrite ships venue names in its ItemList
    assert any("venue_name" in p for p in payloads)


def test_ics_fixture():
    payloads = ics.parse((FIXTURES / "sample.ics").read_bytes())
    assert len(payloads) == 2  # the date-less VEVENT is dropped
    yoga = payloads[0]
    assert yoga["title"]["value"] == "Yoga im Park"
    starts = parse_dt(yoga["starts_at"]["value"])
    assert starts == datetime(2026, 8, 10, 18, 30, tzinfo=VIENNA)
    assert yoga["venue_name"]["value"] == "Donaupark Linz"
    # RRULE is carried through raw for phase 2, never invented
    schach = payloads[1]
    assert "FREQ=WEEKLY" in schach["rrule_raw"]["value"]


def test_ics_keeps_past_anchor_with_future_recurrence_and_cancellation_label():
    content = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:traunsee@example.at\r
SUMMARY:Traunsee-Jam\r
DTSTART;TZID=Europe/Vienna:20260804T183000\r
DTEND;TZID=Europe/Vienna:20260804T213000\r
RRULE:FREQ=WEEKLY;UNTIL=20260915T163100Z;INTERVAL=2\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:blind@example.at\r
SUMMARY:ABGESAGT!!!!! Blind Jam\r
DTSTART;TZID=Europe/Vienna:20260913T190000\r
END:VEVENT\r
END:VCALENDAR\r
"""
    traunsee, blind = ics.parse(content)

    assert is_upcoming(
        traunsee,
        now=datetime(2026, 8, 12, 12, 0, tzinfo=VIENNA),
    )
    assert blind["title"]["value"] == "Blind Jam"
    assert blind["status"] == {"value": "cancelled", "confidence": 0.95}


def test_ics_preserves_recurrence_exceptions_and_rfc_cancellation():
    content = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:series@example.at\r
SUMMARY:Open Jam\r
DTSTART;TZID=Europe/Vienna:20260804T183000\r
RRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4\r
EXDATE;TZID=Europe/Vienna:20260818T183000\r
RDATE;TZID=Europe/Vienna:20260825T183000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:cancel@example.at\r
SUMMARY:Blind Jam\r
DTSTART;TZID=Europe/Vienna:20260913T190000\r
STATUS:CANCELLED\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:question@example.at\r
SUMMARY:Warum wurde das Konzert ABGESAGT?\r
DTSTART;TZID=Europe/Vienna:20260920T190000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:trailing@example.at\r
SUMMARY:TanzFest ABGESAGT !!! (TanzFest-Team)\r
DTSTART;TZID=Europe/Vienna:20260926T200000\r
END:VEVENT\r
END:VCALENDAR\r
"""
    series, cancelled, question, trailing = ics.parse(content)

    raw = series["rrule_raw"]["value"]
    assert "DTSTART;TZID=Europe/Vienna:20260804T183000" in raw
    assert "EXDATE:20260818T163000Z" in raw
    assert "RDATE:20260825T163000Z" in raw
    assert series["calendar_uid"]["value"] == "series@example.at"
    assert cancelled["status"]["value"] == "cancelled"
    assert "status" not in question
    assert trailing["title"]["value"] == "TanzFest (TanzFest-Team)"
    assert trailing["status"]["value"] == "cancelled"


def test_ics_canonical_recurrence_handles_date_floating_and_custom_tzid():
    content = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:date@example.at\r
SUMMARY:All-day series\r
DTSTART;VALUE=DATE:20260801\r
RRULE:FREQ=WEEKLY;COUNT=5\r
EXDATE;VALUE=DATE:20260815\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:floating@example.at\r
SUMMARY:Floating series\r
DTSTART:20260801T180000\r
RRULE:FREQ=WEEKLY;COUNT=5\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:windows@example.at\r
SUMMARY:Windows timezone series\r
DTSTART;TZID=W. Europe Standard Time:20260801T180000\r
RRULE:FREQ=WEEKLY;COUNT=5\r
END:VEVENT\r
END:VCALENDAR\r
"""
    claims = ics.parse(content)
    now = datetime(2026, 8, 12, 12, 0, tzinfo=VIENNA)

    assert all(is_upcoming(claim, now=now) for claim in claims)
    all_day_raw = claims[0]["rrule_raw"]["value"]
    assert "DTSTART;TZID=Europe/Vienna:20260801T000000" in all_day_raw
    assert "EXDATE:20260814T220000Z" in all_day_raw
    assert all(
        "DTSTART;TZID=Europe/Vienna:20260801T180000"
        in claim["rrule_raw"]["value"]
        for claim in claims[1:]
    )


def test_ics_retains_this_and_future_and_rdate_period_start():
    content = b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:range@example.at\r
SUMMARY:Open Jam\r
DTSTART;TZID=Europe/Vienna:20260804T183000\r
RDATE;VALUE=PERIOD:20260818T163000Z/20260818T193000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:range@example.at\r
SUMMARY:Open Jam\r
RECURRENCE-ID;RANGE=THISANDFUTURE;TZID=Europe/Vienna:20260818T183000\r
DTSTART;TZID=Europe/Vienna:20260818T200000\r
END:VEVENT\r
END:VCALENDAR\r
"""
    master, exception = ics.parse(content)

    assert "RDATE:20260818T163000Z" in master["rrule_raw"]["value"]
    assert exception["calendar_uid"]["value"] == "range@example.at"
    assert exception["recurrence_id"]["value"] == "2026-08-18T18:30:00+02:00"
    assert exception["recurrence_range"]["value"] == "this_and_future"
    assert exception["starts_at"]["value"] == "2026-08-18T20:00:00+02:00"


def test_ended_ics_recurrence_is_not_upcoming():
    payload = {
        "starts_at": {"value": "2026-07-01T18:00:00+02:00"},
        "rrule_raw": {
            "value": (
                "DTSTART;TZID=Europe/Vienna:20260701T180000\n"
                "RRULE:FREQ=WEEKLY;UNTIL=20260731T160000Z"
            ),
        },
    }
    assert not is_upcoming(
        payload,
        now=datetime(2026, 8, 12, 12, 0, tzinfo=VIENNA),
    )


def test_past_structured_master_cancellation_survives_upcoming_gate():
    payload = ics.parse(b"""BEGIN:VCALENDAR\r
VERSION:2.0\r
BEGIN:VEVENT\r
UID:cancelled-series@example.at\r
SUMMARY:ABGESAGT Open Jam\r
DTSTART;TZID=Europe/Vienna:20260601T183000\r
STATUS:CANCELLED\r
END:VEVENT\r
END:VCALENDAR\r
""")[0]

    assert is_upcoming(
        payload,
        now=datetime(2026, 8, 12, 12, 0, tzinfo=VIENNA),
    )


def test_linztermine_fixture():
    content = (FIXTURES / "linztermine_sample.xml").read_bytes()
    fake_now = datetime(2026, 7, 3, 8, 0, tzinfo=VIENNA)
    payloads = linztermine.parse(content, now=fake_now)
    assert len(payloads) > 5  # 4 events, one claim per date in horizon
    markt = next(p for p in payloads if "Grünmarkt" in p["title"]["value"])
    assert markt["venue_name"]["value"] == "Grünmarkt Urfahr"
    assert markt["category"]["value"] == "market"
    free = next(p for p in payloads if "Südbahnhofmarkt" in p["title"]["value"])
    assert free["price_min"]["value"] == 0.0  # freeofcharge="1"
    # umlauts survived the declared-latin1-but-actually-utf8 quirk
    assert "ü" in markt["title"]["value"]
    # horizon: nothing older than a day before fake_now, nothing past 60d
    for p in payloads:
        dt = parse_dt(p["starts_at"]["value"])
        assert (fake_now - dt).days <= 1
        assert (dt - fake_now).days <= 60


def test_explicit_concert_start_beats_box_office_time():
    payload = {
        "title": {"value": "Abendmusik in der", "confidence": 0.95},
        "starts_at": {
            "value": "2026-07-20T19:00:00+02:00", "confidence": 0.95,
        },
        "description": {
            "value": (
                "Karten an der Abendkasse ab 19:00 Uhr. Einlass: 19:30 Uhr. "
                "Konzertbeginn: 20:00 Uhr."
            ),
            "confidence": 0.95,
        },
    }

    normalize_claim(payload)

    assert parse_dt(payload["starts_at"]["value"]) == datetime(
        2026, 7, 20, 20, 0, tzinfo=VIENNA,
    )


def test_parse_dt_accepts_german_listing_dates_and_ranges():
    assert parse_dt("21. Jul. 2026, 09:00 Uhr") == datetime(
        2026, 7, 21, 9, 0, tzinfo=VIENNA,
    )
    assert parse_dt("3. März 2027, 18:30 Uhr") == datetime(
        2027, 3, 3, 18, 30, tzinfo=VIENNA,
    )
    assert parse_dt("21. Jul. 2026 – 22. Jul. 2026") == datetime(
        2026, 7, 21, 0, 0, tzinfo=VIENNA,
    )


def test_pdf_fixture_text_extraction():
    from eventindex.extract import pdf

    content = (FIXTURES / "programm.pdf").read_bytes()
    assert pdf.is_pdf(content)
    assert pdf.is_pdf(b"junk", "application/pdf")
    assert not pdf.is_pdf(b"<html>", "text/html")
    text = pdf.to_text(content)
    assert "Sommerkonzert im Pfarrsaal" in text
    assert "07.08.2030" in text
    # malformed bytes must never raise
    assert pdf.to_text(b"%PDF-1.4 garbage") == ""


def test_cascade_routes_pdf_to_llm_tier(conn, monkeypatch):
    from eventindex.extract import extract, field, llm_text

    seen = {}

    def fake_llm(tx, text, source, job_id=None, observed_url=None):
        seen["text"] = text
        seen["url"] = observed_url
        return [{"title": field("Sommerkonzert im Pfarrsaal", 0.8),
                 "starts_at": field("2030-08-07T19:30", 0.8)}]

    monkeypatch.setattr(llm_text, "extract", fake_llm)

    class R:
        content = (FIXTURES / "programm.pdf").read_bytes()
        content_type = "application/pdf"
        url = "https://pfarre.example/programm.pdf"

    source = {"id": None, "kind": "website", "name": "Pfarre St. Anton",
              "lat": None, "lon": None}
    method, payloads = extract(source, R(), conn)
    assert method == "pdf"
    assert "Sommerkonzert" in seen["text"]
    assert seen["url"] == "https://pfarre.example/programm.pdf"
    assert payloads[0]["title"]["value"] == "Sommerkonzert im Pfarrsaal"
