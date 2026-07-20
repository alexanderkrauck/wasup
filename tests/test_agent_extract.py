"""Tier-D extraction rung (fence fired 2026-07-20): the agent's emit_events
path, the vision extractor, the agent_extract handler, and the recipe-level
poster path. LLM/vision stubbed - offline tests prove routing and contracts;
the live ladder suite proves capability."""

import json
import uuid
from datetime import datetime, timedelta, timezone

from eventindex.discovery import onboard
from eventindex.extract import vision
from eventindex.extract.llm_text import LLMEvent, LLMExtraction
from eventindex.jobs import handlers
from eventindex.jobs.worker import enqueue

NOW = datetime.now(timezone.utc)
FUTURE = (NOW + timedelta(days=30)).strftime("%Y-%m-%dT19:00:00")


def _event(title="Sommerkonzert", starts=FUTURE, **kw) -> dict:
    base = {name: None for name in LLMEvent.model_fields}
    return base | {"title": title, "starts_at": starts, "confidence": 0.9} | kw


def test_emit_events_validates_and_collects():
    source = {"id": None, "name": "Pfarre", "lat": None, "lon": None}
    result = onboard.OnboardResult()
    obs = onboard._accept_events(
        {"events": [_event(),
                    _event(title="Vergangenes", starts="2020-01-01"),
                    _event(title="Pfarre Events")]},  # placeholder title
        source, result)
    assert len(result.payloads) == 1
    assert result.payloads[0]["title"]["value"] == "Sommerkonzert"
    assert "accepted 1/3" in obs


def test_emit_events_rejects_garbage_without_raising():
    result = onboard.OnboardResult()
    obs = onboard._accept_events(
        {"events": [{"title": 42}]}, {"id": None, "name": "X"}, result)
    assert "schema invalid" in obs
    assert result.payloads == []


def test_vision_extract_builds_data_url_and_payloads(conn, monkeypatch):
    seen = {}

    def fake_complete(tx, prompt, schema, **kw):
        seen.update(kw)
        return LLMExtraction(events=[LLMEvent.model_validate(_event())])

    monkeypatch.setattr(vision.llm, "complete", fake_complete)
    payloads = vision.extract_image(conn, b"\x89PNGfake", "image/png",
                                    {"id": None}, job_id=None)
    assert payloads[0]["title"]["value"] == "Sommerkonzert"
    assert seen["images"][0].startswith("data:image/png;base64,")
    assert seen["model"] == vision.config.MODEL_VISION
    # oversized and empty images never reach the model
    assert vision.extract_image(conn, b"", "image/png", {"id": None}) == []
    assert vision.extract_image(
        conn, b"x" * (vision.MAX_IMAGE_BYTES + 1), "image/png", {"id": None}) == []


def _fake_session(payloads=None, recipe=None, expected=None, summary=""):
    def fake(tx, source, job_id, model, task_reason=None,
             min_horizon_days=None, mode="recipe"):
        assert mode == "extract"
        return onboard.OnboardResult(
            recipe=recipe, payloads=payloads or [],
            expected_events=expected, summary=summary)
    return fake


def test_agent_extract_inserts_claims_and_goes_agentic(conn, monkeypatch):
    from eventindex.extract import field

    sid = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, status) "
        "VALUES ('Poster Cafe', 'https://p.at', 'website', 3, 0.5, 'degraded') "
        "RETURNING id"
    ).fetchone()["id"]
    payloads = [{"title": field("Sommerkonzert", 0.8),
                 "starts_at": field(FUTURE, 0.8)}]
    monkeypatch.setattr(handlers, "config", handlers.config)
    import eventindex.discovery.onboard as onboard_mod
    monkeypatch.setattr(onboard_mod, "onboard_source",
                        _fake_session(payloads=payloads, expected=5,
                                      summary="events live on the poster wall"))
    with conn.transaction():
        enqueue(conn, "agent_extract", {"source_id": str(sid)})
    job = conn.execute("SELECT * FROM jobs").fetchone()
    job = dict(job) | {"attempts": 1}
    new_jobs = handlers.agent_extract(job, conn)

    claims = conn.execute(
        "SELECT payload FROM event_claim WHERE source_id = %s", (sid,)
    ).fetchall()
    assert len(claims) == 1
    src = conn.execute("SELECT * FROM source WHERE id = %s", (sid,)).fetchone()
    assert src["status"] == "active"
    hint = src["extraction_hint"]
    assert hint["mode"] == "agentic"
    assert hint["agent_yield"] == 1
    assert hint["expected_events"] == 5
    assert hint["onboard_notes"][0] == "events live on the poster wall"
    assert {j["kind"] for j in new_jobs} == {"resolve"}
    log = conn.execute(
        "SELECT detail FROM crawl_log WHERE source_id = %s", (sid,)
    ).fetchone()
    assert "method=agent" in log["detail"]


def test_agent_extract_with_recipe_returns_to_rung_one(conn, monkeypatch):
    from eventindex.fetch.recipe import Pagination, Recipe

    sid = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, status, "
        "extraction_hint) VALUES ('API Site', 'https://a.at', 'website', 3, "
        "0.5, 'degraded', '{\"mode\": \"agentic\"}') RETURNING id"
    ).fetchone()["id"]
    recipe = Recipe(entry_urls=["https://a.at/api/events"],
                    pagination=Pagination(type="none"))
    import eventindex.discovery.onboard as onboard_mod
    monkeypatch.setattr(onboard_mod, "onboard_source",
                        _fake_session(recipe=recipe, expected=12))
    job = {"id": uuid.uuid4(), "attempts": 1,
           "payload": {"source_id": str(sid)}}
    new_jobs = handlers.agent_extract(job, conn)

    src = conn.execute("SELECT * FROM source WHERE id = %s", (sid,)).fetchone()
    assert src["status"] == "active"
    assert src["recipe"]["entry_urls"] == ["https://a.at/api/events"]
    assert "mode" not in src["extraction_hint"]  # de-escalated to rung 1
    assert src["extraction_hint"]["expected_events"] == 12
    assert {j["kind"] for j in new_jobs} == {"crawl"}


def test_recipe_image_selector_extracts_and_caches(conn, monkeypatch):
    from eventindex.extract import field
    from eventindex.fetch import recipe as recipe_mod
    from eventindex.fetch.recipe import Pagination, Recipe, run_recipe

    sid = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust) VALUES "
        "('Poster Venue', 'https://pv.at', 'website', 3, 0.5) RETURNING id"
    ).fetchone()["id"]
    source = dict(conn.execute(
        "SELECT *, NULL AS lat, NULL AS lon FROM source WHERE id = %s", (sid,)
    ).fetchone())

    html = (b"<html><body><div class='events'>"
            b"<img class='poster' src='/august.jpg'>"
            b"<p>Programm als Bild</p></div></body></html>")
    calls = {"vision": 0, "img": 0}

    class _Resp:
        content = b"fake-jpeg-bytes"
        headers = {"content-type": "image/jpeg"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(recipe_mod, "MAX_NEW_IMAGES_PER_CRAWL", 5)
    monkeypatch.setattr("httpx.get",
                        lambda *a, **kw: calls.__setitem__("img", calls["img"] + 1) or _Resp())
    import eventindex.extract.vision as vision_mod

    def fake_vision(tx, image, mime, src, job_id=None):
        calls["vision"] += 1
        return [{"title": field("Augustkonzert", 0.8),
                 "starts_at": field(FUTURE, 0.8)}]

    monkeypatch.setattr(vision_mod, "extract_image", fake_vision)
    monkeypatch.setattr(recipe_mod.config, "CRAWL_DELAY_S", 0)

    recipe = Recipe(entry_urls=["https://pv.at/programm"],
                    pagination=Pagination(type="none"),
                    image_selector="img.poster",
                    validation={"min_items": 1})
    payloads, validation = run_recipe(recipe, source, conn,
                                      fetch_page=lambda url: html)
    assert validation.ok
    assert payloads[0]["title"]["value"] == "Augustkonzert"
    assert calls["vision"] == 1

    # second crawl: same poster bytes -> sha cache -> no second vision call
    source = dict(conn.execute(
        "SELECT *, NULL AS lat, NULL AS lon FROM source WHERE id = %s", (sid,)
    ).fetchone())
    assert source["extraction_hint"]["image_seen"]
    payloads2, _ = run_recipe(recipe, source, conn,
                              fetch_page=lambda url: html)
    assert calls["vision"] == 1
    assert payloads2 == []


def _recipe_source(conn, hint):
    from psycopg.types.json import Jsonb

    recipe = {"entry_urls": ["https://x.at/events"],
              "pagination": {"type": "none"}, "version": 1}
    return conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, recipe, yield_ema, "
        "extraction_hint) VALUES ('X', 'https://x.at', 'website', 3, 0.5, "
        "%s, 4, %s) RETURNING id",
        (Jsonb(recipe), Jsonb(hint)),
    ).fetchone()["id"]


def test_low_yield_recipe_escalates_to_agent_after_two_strikes(conn, monkeypatch):
    from eventindex.extract import field
    from eventindex.fetch.recipe import ValidationResult

    few = [{"title": field("Einzelkonzert", 0.8),
            "starts_at": field(FUTURE, 0.8)}] * 3
    monkeypatch.setattr(
        handlers, "run_recipe",
        lambda recipe, source, tx, job_id=None: (
            few, ValidationResult(ok=True, items=3, reasons=[], pages=1)),
    )
    sid = _recipe_source(conn, {"expected_events": 20, "low_yield_count": 1})
    job = {"id": uuid.uuid4(), "attempts": 1, "payload": {"source_id": str(sid)}}
    new_jobs = handlers.crawl(job, conn)
    kinds = {j["kind"] for j in new_jobs}
    assert "agent_extract" in kinds
    reason = next(j for j in new_jobs if j["kind"] == "agent_extract")
    assert "completeness" in reason["payload"]["reason"]


def test_low_yield_escalation_respects_cooldown(conn, monkeypatch):
    from eventindex.extract import field
    from eventindex.fetch.recipe import ValidationResult

    few = [{"title": field("Einzelkonzert", 0.8),
            "starts_at": field(FUTURE, 0.8)}] * 3
    monkeypatch.setattr(
        handlers, "run_recipe",
        lambda recipe, source, tx, job_id=None: (
            few, ValidationResult(ok=True, items=3, reasons=[], pages=1)),
    )
    recent = NOW.isoformat(timespec="seconds")
    sid = _recipe_source(conn, {"expected_events": 20, "low_yield_count": 1,
                                "last_agent_extract": recent})
    job = {"id": uuid.uuid4(), "attempts": 1, "payload": {"source_id": str(sid)}}
    new_jobs = handlers.crawl(job, conn)
    assert all(j["kind"] != "agent_extract" for j in new_jobs)


def test_parity_audit_measures_coverage_and_feeds_notes(conn, monkeypatch):
    from psycopg.types.json import Jsonb

    from eventindex.extract import field
    from eventindex.resolve.fingerprint import fingerprint

    recipe = {"entry_urls": ["https://v.at/events"],
              "pagination": {"type": "none"}, "version": 1}
    sid = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust, yield_ema, recipe) "
        "VALUES ('Vereinsheim', 'https://v.at', 'website', 3, 0.5, 10, %s) "
        "RETURNING id",
        (Jsonb(recipe),),
    ).fetchone()["id"]
    # the recipe already knows one of the two events the agent will find
    known_fp = fingerprint("Bekanntes Konzert",
                           datetime.fromisoformat(FUTURE).astimezone(timezone.utc),
                           lat=None, lon=None)
    conn.execute(
        "INSERT INTO event_claim (source_id, fingerprint, payload) "
        "VALUES (%s, %s, %s)",
        (sid, known_fp, Jsonb({"title": {"value": "Bekanntes Konzert",
                                         "confidence": 0.8},
                               "starts_at": {"value": FUTURE,
                                             "confidence": 0.8}})),
    )
    payloads = [
        {"title": field("Bekanntes Konzert", 0.8), "starts_at": field(FUTURE, 0.8)},
        {"title": field("Poster-Geheimtipp", 0.8), "starts_at": field(FUTURE, 0.8)},
    ]
    import eventindex.discovery.onboard as onboard_mod

    def fake(tx, source, job_id, model, task_reason=None,
             min_horizon_days=None, mode="recipe"):
        assert mode == "extract" and "parity audit" in task_reason
        return onboard.OnboardResult(payloads=payloads)

    monkeypatch.setattr(onboard_mod, "onboard_source", fake)
    job = {"id": uuid.uuid4(), "attempts": 1, "payload": {"sample": 3}}
    new_jobs = handlers.parity_audit(job, conn)

    log = conn.execute(
        "SELECT detail FROM crawl_log WHERE source_id = %s "
        "AND detail LIKE 'parity%%'", (sid,)
    ).fetchone()["detail"]
    assert "agent=2 known=1 coverage=0.50" in log
    assert "Poster-Geheimtipp" in log
    notes = conn.execute(
        "SELECT extraction_hint->'onboard_notes' AS n FROM source WHERE id = %s",
        (sid,),
    ).fetchone()["n"]
    assert "Poster-Geheimtipp" in notes[0]
    claims = conn.execute(
        "SELECT count(*) AS n FROM event_claim WHERE source_id = %s", (sid,)
    ).fetchone()["n"]
    assert claims == 3  # the agent's finds are claims: index converges now
    assert {j["kind"] for j in new_jobs} == {"resolve"}
