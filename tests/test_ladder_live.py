"""Live extraction-ladder gate (fence fired 2026-07-20): the REAL agent, the
REAL models, deterministic local fixture sites. This is the falsifiable form
of the human-parity requirement - every modality class here must extract, or
the build is red.

Marked `live`: spends real LLM budget (~EUR 0.3-0.8 total). Uses the test DB
(conn fixture). Run:

    uv run pytest -m live tests/test_ladder_live.py -v
"""

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

from eventindex import config
from eventindex.discovery import onboard
from eventindex.fetch.recipe import Recipe, run_recipe
from eventindex.jobs import handlers

pytestmark = pytest.mark.live

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime.now(timezone.utc)


def _future(days: int, hour: int = 19) -> str:
    return (NOW + timedelta(days=days)).strftime(f"%Y-%m-%dT{hour:02d}:00:00")


EVENTS_JSON = {
    "Page": 1,
    "CalendarEvents": [
        {"Id": 1, "Name": "Werkstattkonzert Quartett Nord",
         "StartDate": _future(12), "Location": "Halle 1, Museumstrasse 12"},
        {"Id": 2, "Name": "Repair-Cafe Linz Mitte",
         "StartDate": _future(19, hour=15), "Location": "Halle 2"},
        {"Id": 3, "Name": "Vortrag: Donauradweg neu gedacht",
         "StartDate": _future(33), "Location": "Seminarraum"},
    ],
}

SPA_SHELL = b"""<!doctype html><html><head><title>Halle Events</title></head>
<body><div id="app"></div>
<script>
fetch('/api/public/events').then(r => r.json()).then(data => {
  const app = document.getElementById('app');
  app.innerHTML = data.CalendarEvents.map(e =>
    '<div class="ev">' + e.Name + ' - ' + e.StartDate + ' - '
    + (e.Location || '') + '</div>').join('');
});
</script></body></html>"""

POSTER_PAGE = (b"<!doctype html><html><head><title>Programm</title></head>"
               b"<body><h1>Unser Sommerprogramm</h1>"
               b"<img id='poster' src='/poster.png' alt='Programm'>"
               b"</body></html>")


@pytest.fixture
def site():
    """One throwaway local site; tests mutate `pages` to simulate change."""
    pages = {
        "/events/list": (SPA_SHELL, "text/html"),
        "/api/public/events": (json.dumps(EVENTS_JSON).encode(),
                               "application/json"),
        "/programm": (POSTER_PAGE, "text/html"),
        "/poster.png": ((FIXTURES / "poster.png").read_bytes(), "image/png"),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body, ctype = pages.get(self.path.split("?")[0], (b"nope", "text/html"))
            self.send_response(200 if body != b"nope" else 404)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    yield base, pages
    server.shutdown()


@pytest.fixture
def fast_rings(monkeypatch):
    """Fixture sites are trivial; a lost session must fail fast, not burn."""
    monkeypatch.setattr(config, "ONBOARD_MAX_TURNS", 14)
    monkeypatch.setattr(config, "ONBOARD_WALL_CLOCK_S", 420)
    monkeypatch.setattr(config, "ONBOARD_SESSION_CAP_EUR", 0.40)
    monkeypatch.setattr(config, "CRAWL_DELAY_S", 0.1)


def _source(conn, url, name, **cols):
    fields = {"name": name, "url": url, "kind": "website", "tier": 3,
              "trust": 0.5} | cols
    keys = ", ".join(fields)
    ph = ", ".join(["%s"] * len(fields))
    row = conn.execute(
        f"INSERT INTO source ({keys}) VALUES ({ph}) RETURNING *",
        tuple(Jsonb(v) if isinstance(v, (dict, list)) else v
              for v in fields.values()),
    ).fetchone()
    conn.commit()  # the spend ledger writes on its own connection
    return dict(row) | {"lat": None, "lon": None}


def test_agent_onboards_spa_with_json_api(conn, site, fast_rings):
    """The factory300 class, end to end: JS shell + public JSON API. The
    agent must produce a validating recipe whose crawl reaches >=2/3 of the
    ground-truth events."""
    base, _ = site
    source = _source(conn, f"{base}/events/list", "Halle (SPA fixture)")
    result = onboard.onboard_source(conn, source, None, config.MODEL_MINI)
    assert result.recipe is not None

    payloads, validation = run_recipe(result.recipe, source, conn)
    titles = " | ".join(p["title"]["value"] for p in payloads)
    hits = sum(1 for t in ("Werkstattkonzert", "Repair-Cafe", "Donauradweg")
               if t in titles)
    assert validation.ok
    assert hits >= 2, f"recipe reached only {hits}/3 events: {titles}"


def test_agent_extracts_poster_events_via_vision(conn, site, fast_rings):
    """The vision class: events published only as an image. The extract-mode
    agent must read the poster and emit >=2 of its 3 events."""
    base, _ = site
    source = _source(conn, f"{base}/programm", "Innenhof (Poster fixture)")
    result = onboard.onboard_source(conn, source, None, config.MODEL_VISION,
                                    mode="extract")
    titles = " | ".join(p["title"]["value"] for p in result.payloads)
    hits = sum(1 for t in ("Sommerkonzert", "Casablanca", "Familienbrunch")
               if t in titles)
    assert hits >= 2, f"vision found only {hits}/3 poster events: {titles}"


def test_broken_recipe_heals_through_the_ladder(conn, site, fast_rings):
    """Death and resurrection (the factory300 regression): a working recipe
    breaks when the site changes; two crawls degrade the source and enqueue
    the agent; the agent extracts (index never dark) and repairs."""
    base, pages = site
    # v1: a plain HTML listing the recipe's selectors understand
    v1 = ("<html><body>" + "".join(
        f"<div class='item'><h3>{e['Name']}</h3>"
        f"<time>{e['StartDate']}</time></div>"
        for e in EVENTS_JSON["CalendarEvents"]) + "</body></html>").encode()
    pages["/events/list"] = (v1, "text/html")
    recipe = {
        "entry_urls": [f"{base}/events/list"], "version": 1,
        "pagination": {"type": "none"},
        "item_scope": "div.item",
        "field_selectors": {"title": "h3", "starts_at": "time"},
        "validation": {"min_items": 2},
    }
    source = _source(conn, f"{base}/events/list", "Halle (heal fixture)",
                     recipe=recipe, yield_ema=3.0)
    job = {"id": None, "attempts": 1, "payload": {"source_id": str(source["id"])}}
    assert not any(j["kind"] == "agent_extract" for j in handlers.crawl(job, conn))

    # the platform relaunches as a JS shell: selectors go blind
    pages["/events/list"] = (SPA_SHELL, "text/html")
    handlers.crawl(job, conn)
    repair_jobs = handlers.crawl(job, conn)
    repairs = [j for j in repair_jobs if j["kind"] == "agent_extract"]
    assert repairs, "two blind crawls must enqueue the agent"
    status = conn.execute("SELECT status FROM source WHERE id = %s",
                          (source["id"],)).fetchone()["status"]
    assert status == "degraded"

    before = conn.execute(
        "SELECT count(*) AS n FROM event_claim WHERE source_id = %s",
        (source["id"],),
    ).fetchone()["n"]
    agent_job = {"id": None, "attempts": 1, "payload": repairs[0]["payload"]}
    handlers.agent_extract(agent_job, conn)

    after = conn.execute(
        "SELECT count(*) AS n FROM event_claim WHERE source_id = %s",
        (source["id"],),
    ).fetchone()["n"]
    healed = conn.execute("SELECT status, recipe, extraction_hint FROM source "
                          "WHERE id = %s", (source["id"],)).fetchone()
    assert healed["status"] == "active"
    # healed = a new recipe (rung 1) or agentic claims (rung 3) - either way
    # the source is alive and the index moved forward
    assert (healed["recipe"]["version"] != 1
            or healed["extraction_hint"].get("mode") == "agentic")
    assert after > before, "the healing session must add claims"
