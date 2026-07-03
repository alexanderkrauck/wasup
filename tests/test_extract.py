"""Extractor regression tests on recorded fixtures (HURDLES H3.4)."""

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from eventindex.extract import ics, jsonld, linztermine, parse_dt

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
