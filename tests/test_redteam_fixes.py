"""Regression tests from the 2026-07-20 red-team audit: each pins the exact
production defect class with the real payloads/wording that slipped through."""

import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from eventindex.extract import is_non_event, sanity_filter
from eventindex.resolve.rebuild import Claim, _recurrence_of

VIENNA = ZoneInfo("Europe/Vienna")


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


def test_internal_source_url_never_becomes_canonical():
    from types import SimpleNamespace

    from eventindex.resolve.rebuild import _fallback_source_url

    qa = SimpleNamespace(source_url="internal://qa-verifier")
    real = SimpleNamespace(source_url="https://pfarre.at/termine")
    # the QA verifier's trust makes it rep - its url must not ship
    assert _fallback_source_url([qa, real], qa) == "https://pfarre.at/termine"
    assert _fallback_source_url([real, qa], real) == "https://pfarre.at/termine"
    assert _fallback_source_url([qa], qa) is None
