"""json_api cascade tier: deterministic JSON record sniffing (fence fired
2026-07-20). Pins the factory300 class: a correct JSON-endpoint recipe must
extract, never fail with "items 0"."""

import json
from pathlib import Path

from eventindex.extract import extract, json_api, parse_dt

FIXTURES = Path(__file__).parent / "fixtures"


class _Result:
    def __init__(self, content: bytes, content_type="text/html",
                 url="https://api.example/events"):
        self.content = content
        self.content_type = content_type
        self.url = url


def test_nexudus_fixture_extracts_all_events():
    payloads = json_api.parse((FIXTURES / "nexudus_events.json").read_bytes())
    assert len(payloads) == 3
    workshop = payloads[0]
    assert workshop["title"]["value"] == "WORKSHOP - Sag was Sache ist!"
    assert parse_dt(workshop["starts_at"]["value"]) is not None
    assert parse_dt(workshop["ends_at"]["value"]) is not None
    assert workshop["venue_name"]["value"].startswith("factory300")
    # HTML descriptions arrive as text, entities unescaped downstream
    assert "<p>" not in workshop["description"]["value"]
    assert workshop["organizer"]["value"] == "factory300 Team"
    assert workshop["price_min"]["value"] == 49.0
    # the Categories array (title-ish but dateless) must contribute nothing
    titles = [p["title"]["value"] for p in payloads]
    assert "Legal" not in titles and "Community" not in titles


def test_generic_lowercase_wrapper():
    body = json.dumps({"data": {"results": [
        {"title": "Sommerfest", "start_date": "2030-07-01", "url": "https://x.at/1"},
        {"title": "Flohmarkt", "start_date": "2030-07-02", "url": "https://x.at/2"},
    ]}}).encode()
    payloads = json_api.parse(body)
    assert [p["title"]["value"] for p in payloads] == ["Sommerfest", "Flohmarkt"]
    assert payloads[0]["url"]["value"] == "https://x.at/1"


def test_nested_occurrence_arrays_do_not_double_count():
    body = json.dumps({"events": [
        {"name": "Kursreihe", "start": "2030-03-01T10:00",
         "dates": [{"name": "Kursreihe", "start": "2030-03-08T10:00"}]},
    ]}).encode()
    assert len(json_api.parse(body)) == 1


def test_ids_and_numbers_are_not_dates():
    body = json.dumps({"items": [
        {"name": "Not an event", "start": "1415748758"},
        {"name": "Also not", "start": 20300101},
    ]}).encode()
    assert json_api.parse(body) == []


def test_non_json_bodies_yield_nothing():
    assert json_api.parse(b"<html><body>Konzert am 5.8.</body></html>") == []
    assert json_api.parse(b"<?xml version='1.0'?><events/>") == []


def test_cascade_routes_json_before_llm(conn):
    source = {"id": None, "kind": "website", "name": "API Source",
              "lat": None, "lon": None}
    body = json.dumps({"events": [
        {"name": "Gartenkonzert", "startdate": "2030-08-01T19:00"},
        {"name": "Keramikmarkt", "startdate": "2030-08-02T09:00"},
    ]}).encode()
    method, payloads = extract(source, _Result(body), conn)
    assert method == "json_api"
    assert [p["title"]["value"] for p in payloads] == ["Gartenkonzert", "Keramikmarkt"]


def test_cascade_ignores_json_without_events(conn, monkeypatch):
    from eventindex.extract import llm_text

    monkeypatch.setattr(llm_text, "extract",
                        lambda tx, text, source, job_id=None,
                        observed_url=None: [])
    source = {"id": None, "kind": "website", "name": "X",
              "lat": None, "lon": None}
    body = json.dumps({"config": {"locale": "de"}}).encode()
    method, payloads = extract(source, _Result(body), conn)
    assert method == "llm"
    assert payloads == []
