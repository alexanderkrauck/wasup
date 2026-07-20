"""Extractor regression tests on recorded fixtures (HURDLES H3.4)."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from eventindex.extract import ics, jsonld, linztermine, normalize_claim, parse_dt

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

    def fake_llm(tx, text, source, job_id=None):
        seen["text"] = text
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
    assert payloads[0]["title"]["value"] == "Sommerkonzert im Pfarrsaal"
