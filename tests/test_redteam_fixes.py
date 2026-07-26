"""Regression tests from the 2026-07-20 red-team audit: each pins the exact
production defect class with the real payloads/wording that slipped through."""

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from eventindex.extract import is_non_event, sanity_filter
from eventindex.resolve.rebuild import Claim, _recurrence_of

VIENNA = ZoneInfo("Europe/Vienna")


def _field_confidences(llm_text, **values):
    return dict.fromkeys(llm_text.LLMFieldConfidences.model_fields) | {
        "title": 0.9, "starts_at": 0.9,
    } | values


def _claim(title, starts, recurrence):
    return Claim(
        id=uuid.uuid4(), source_id=uuid.uuid4(), fingerprint="fp",
        extracted_at=datetime.now(timezone.utc),
        payload={"title": {"value": title},
                 "starts_at": {"value": starts.isoformat()},
                 "recurrence": {"value": recurrence}},
        trust=0.8, source_url="https://x.at", source_lat=None,
        source_lon=None, title=title, starts_at=starts,
    )


_REC = {"freq": "daily", "weekday": None, "week_of_month": None,
        "interval": 1, "time": "08:00", "duration_minutes": None,
        "except_holidays": [], "valid_from": None, "valid_until": None,
        "as_stated": "täglich in Christkönig"}


def test_recurrence_of_keeps_rules_for_the_verifier():
    # weekdays are not special (Alexander 2026-07-20): no vocabulary gate
    # here - rule-vs-event coherence is the H1.1 verifier's judgment
    friday = datetime(2026, 7, 17, 8, 0, tzinfo=VIENNA)
    c = _claim("Wochentagsmesse (Eucharistiefeier) - Freitag", friday, _REC)
    rec = _recurrence_of(c)
    assert rec is not None and rec.freq == "daily"


def test_verifier_judges_rule_against_event_context(monkeypatch):
    from types import SimpleNamespace

    from eventindex.resolve import recurrence as rec_mod

    seen = {}

    def fake_complete(tx, prompt, schema, **kw):
        seen["prompt"] = prompt
        return SimpleNamespace(consistent=False)

    monkeypatch.setattr("eventindex.llm.complete", fake_complete)
    rec = rec_mod.Recurrence.model_validate(_REC)
    friday = datetime(2026, 7, 17, 8, 0, tzinfo=VIENNA)
    tuesday = datetime(2026, 7, 21, 8, 0, tzinfo=VIENNA)
    ok = rec_mod.verify(None, rec, [tuesday, friday],
                        title="Wochentagsmesse (Eucharistiefeier) - Freitag",
                        anchor=friday)
    assert ok is False
    # the verdict weighs the event itself, not only as_stated
    assert "Wochentagsmesse (Eucharistiefeier) - Freitag" in seen["prompt"]
    assert "Friday" in seen["prompt"]      # the anchor, weekday spelled out
    assert "Tuesday" in seen["prompt"]     # the contradicting expansion


def test_announcements_are_non_events():
    # the live Stahlwelt defect
    assert is_non_event("Wiedereröffnung der voestalpine Stahlwelt - Touren ab 13. Juli")
    assert is_non_event("Neueröffnung im Zentrum")
    assert is_non_event("Wir haben jetzt wieder geöffnet")
    # a dated celebration stays an event (German compound keeps the boundary)
    assert not is_non_event("Wiedereröffnungsfeier mit Livemusik")
    assert not is_non_event("Sommerkonzert der Stadtkapelle")


def test_junk_urls_dropped_at_claim_hygiene():
    # live corpus 2026-07-21: "Keine URL", an e-mail address, bare domains
    # and zoom paths without scheme shipped as event links (65 events)
    from eventindex.extract import normalize_claim

    def payload(url):
        return {"title": {"value": "Konzert", "confidence": 0.9},
                "url": {"value": url, "confidence": 0.9}}

    for junk in ("Keine URL", "info@utsc-linz.at", "tfc-twisters.at",
                 "zoom.us/j/99847510499", "oökultur.at/x"):
        assert "url" not in normalize_claim(payload(junk)), junk
    kept = normalize_claim(payload("https://posthof.at/x"))
    assert kept["url"]["value"] == "https://posthof.at/x"


def test_llm_text_drops_invented_urls(monkeypatch):
    # live: a fabricated linz-termine slug (301-loop, never existed on the
    # page) shipped as canonical URL - text-tier urls must appear in the text
    from eventindex.extract import llm_text

    text = ("Workshop Extremismus am 12.09.2026 im Wissensturm. " * 4
            + "Anmeldung: https://linztermine.at/kurs/123 ")
    evidence = dict.fromkeys(llm_text.LLMEventEvidence.model_fields) | {
        "event_excerpt": "Workshop Extremismus am 12.09.2026 im Wissensturm.",
        "title": "Workshop Extremismus",
        "starts_at": "12.09.2026",
        "url": "https://www.linz-termine.at/event/extremismus-basisworkshop",
        "booking_url": "https://linztermine.at/kurs/123",
    }
    ev = dict.fromkeys(llm_text.LLMEvent.model_fields) | {
        "title": "Workshop Extremismus", "starts_at": "2026-09-12",
        "url": "https://www.linz-termine.at/event/extremismus-basisworkshop",
        "booking_url": "https://linztermine.at/kurs/123", "confidence": 0.9,
        "evidence": evidence,
        "field_confidences": _field_confidences(
            llm_text, url=0.9, booking_url=0.9,
        ),
    }
    extraction = llm_text.LLMExtraction(events=[llm_text.LLMEvent(**ev)])
    monkeypatch.setattr("eventindex.llm.complete", lambda *a, **k: extraction)
    payloads = llm_text.extract(None, text, {"id": None})
    assert "url" not in payloads[0]                      # invented -> dropped
    assert payloads[0]["booking_url"]["value"] == "https://linztermine.at/kurs/123"


def test_llm_text_rejects_identity_date_not_supported_by_source(monkeypatch):
    """The exact Linz-Marathon failure class: plausible title, wrong month."""
    from eventindex.extract import llm_text

    text = (
        "24. Oberbank Linz Donau Marathon 2026 findet am 12. April 2026 statt. "
        * 4
    )
    evidence = dict.fromkeys(llm_text.LLMEventEvidence.model_fields) | {
        "event_excerpt": (
            "24. Oberbank Linz Donau Marathon 2026 findet am "
            "12. April 2026 statt."
        ),
        "title": "24. Oberbank Linz Donau Marathon 2026",
        "starts_at": "12. April 2026",
    }
    event = dict.fromkeys(llm_text.LLMEvent.model_fields) | {
        "title": "24. Oberbank Linz Donau Marathon 2026",
        "starts_at": "2026-08-02",
        "confidence": 0.9,
        "evidence": evidence,
        "field_confidences": _field_confidences(llm_text),
    }
    extraction = llm_text.LLMExtraction(
        events=[llm_text.LLMEvent(**event)]
    )
    monkeypatch.setattr("eventindex.llm.complete", lambda *a, **k: extraction)

    assert llm_text.extract(None, text, {"id": None}) == []


def test_llm_text_drops_only_unsupported_optional_fields(monkeypatch):
    from eventindex.extract import llm_text

    text = (
        "Sommernachtstanz am 18. Juli 2027 um 20:30 im Stadtpark. "
        "Eintritt 12 EUR. " * 4
    )
    evidence = dict.fromkeys(llm_text.LLMEventEvidence.model_fields) | {
        "event_excerpt": (
            "Sommernachtstanz am 18. Juli 2027 um 20:30 im Stadtpark."
        ),
        "title": "Sommernachtstanz",
        "starts_at": "18. Juli 2027 um 20:30",
        "venue_name": "im Stadtpark",
        "price_min": "Eintritt 12 EUR",
    }
    event = dict.fromkeys(llm_text.LLMEvent.model_fields) | {
        "title": "Sommernachtstanz",
        "starts_at": "2027-07-18T20:30:00+02:00",
        "venue_name": "Stadtpark",
        "price_min": 12,
        "organizer": "Erfundene GmbH",
        "confidence": 0.8,
        "evidence": evidence,
        "field_confidences": _field_confidences(
            llm_text, venue_name=0.8, price_min=0.7, organizer=0.6,
        ),
    }
    extraction = llm_text.LLMExtraction(
        events=[llm_text.LLMEvent(**event)]
    )
    monkeypatch.setattr("eventindex.llm.complete", lambda *a, **k: extraction)

    payload = llm_text.extract(
        None, text, {"id": None}, observed_url="https://events.example/list",
    )[0]
    assert payload["starts_at"]["value"].startswith("2027-07-18T20:30")
    assert payload["venue_name"]["value"] == "Stadtpark"
    assert payload["price_min"]["value"] == 12
    assert "organizer" not in payload
    assert payload["title"]["event_excerpt"].startswith("Sommernachtstanz")
    assert payload["title"]["observed_url"] == "https://events.example/list"


def test_html_to_text_preserves_absolute_link_evidence():
    from eventindex.extract.llm_text import html_to_text

    text = html_to_text(
        b'<a href="/tickets/42">Tickets</a>',
        "https://events.example/program",
    )
    assert "https://events.example/tickets/42" in text


def test_clamped_validity_ranges_yield_no_occurrences():
    # the 695-row midnight cluster: linztermine clamps a series' validity
    # range to the VIEWING day, so daily crawls leave contradictory "first
    # days" (Sun 12.07, Mon 13.07 for a Tuesday series) - none is observed
    from eventindex.resolve.rebuild import _claim_cands

    def claim(starts, ends):
        c = _claim("Dienstagabend im Mariendom", starts, None)
        c.ends_at = ends
        return c

    range_end = datetime(2026, 9, 8, 0, 0, tzinfo=VIENNA)
    clamped = [
        claim(datetime(2026, 7, 12, 0, 0, tzinfo=VIENNA), range_end),
        claim(datetime(2026, 7, 13, 0, 0, tzinfo=VIENNA), range_end),
    ]
    assert _claim_cands(clamped) == []
    # a SINGLE range claim may state the real first date (Jazz im
    # Musikpavillon) - it stays
    assert len(_claim_cands(clamped[:1])) == 1
    # timed claims in the same group are untouched
    tue = datetime(2026, 7, 14, 19, 30, tzinfo=VIENNA)
    cands = _claim_cands(clamped + [claim(tue, tue + timedelta(minutes=45))])
    assert cands == [(tue, tue + timedelta(minutes=45), True)]


def test_internal_source_url_never_becomes_canonical():
    from types import SimpleNamespace

    from eventindex.resolve.rebuild import _fallback_source_url

    qa = SimpleNamespace(source_url="internal://qa-verifier")
    real = SimpleNamespace(source_url="https://pfarre.at/termine")
    # the QA verifier's trust makes it rep - its url must not ship
    assert _fallback_source_url([qa, real], qa) == "https://pfarre.at/termine"
    assert _fallback_source_url([real, qa], real) == "https://pfarre.at/termine"
    assert _fallback_source_url([qa], qa) is None
