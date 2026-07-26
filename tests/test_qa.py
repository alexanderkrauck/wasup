"""qa_check handler: trust EMA feedback, confirmation timestamps, and
cancellation via a claim (H0: canon is never edited directly)."""

import uuid
from datetime import datetime, timedelta, timezone

from psycopg.types.json import Jsonb

from eventindex.jobs import handlers

NOW = datetime.now(timezone.utc)


def _setup(conn, trust=0.8):
    sid = conn.execute(
        "INSERT INTO source (name, url, kind, tier, trust) "
        "VALUES ('venue-site', %s, 'website', 2, %s) RETURNING id",
        (f"https://{uuid.uuid4().hex[:10]}.at", trust),
    ).fetchone()["id"]
    eid = uuid.uuid4()
    conn.execute(
        "INSERT INTO event (id, kind, title, url, confidence, status) "
        "VALUES (%s, 'one_off', 'Konzert X', 'https://venue.at/x', 0.9, 'confirmed')",
        (eid,),
    )
    oid = conn.execute(
        "INSERT INTO occurrence (event_id, starts_at) VALUES (%s, %s) RETURNING id",
        (eid, NOW + timedelta(days=2)),
    ).fetchone()["id"]
    fp = f"konzert x|2026-07-20|{uuid.uuid4().hex[:6]}"
    conn.execute(
        "INSERT INTO identity (fingerprint, event_id) VALUES (%s, %s)", (fp, eid)
    )
    conn.execute(
        "INSERT INTO event_claim (source_id, fingerprint, payload) VALUES (%s, %s, %s)",
        (sid, fp, Jsonb({"title": {"value": "Konzert X", "confidence": 0.9},
                         "starts_at": {"value": (NOW + timedelta(days=2)).isoformat(),
                                       "confidence": 0.9}})),
    )
    return sid, eid, oid


def _job(oid):
    return {"id": uuid.uuid4(), "payload": {"occurrence_id": str(oid)}}


def test_confirmed_bumps_trust_and_timestamp(conn, monkeypatch):
    sid, _, oid = _setup(conn)
    monkeypatch.setattr(handlers, "_qa_verify", lambda tx, occ, job_id: "confirmed")
    assert handlers.qa_check(_job(oid), conn) == []

    src = conn.execute("SELECT trust FROM source WHERE id = %s", (sid,)).fetchone()
    assert abs(src["trust"] - (0.8 * 0.9 + 0.1)) < 1e-9  # 0.82
    occ = conn.execute(
        "SELECT last_confirmed_at FROM occurrence WHERE id = %s", (oid,)
    ).fetchone()
    assert occ["last_confirmed_at"] is not None
    # the confirmation is also a claim - only claims survive rebuilds (H0)
    claim = conn.execute(
        "SELECT c.payload FROM event_claim c JOIN source s ON s.id = c.source_id "
        "WHERE s.url = %s", (handlers.QA_SOURCE_URL,),
    ).fetchone()
    assert claim is not None and "status" not in claim["payload"]
    log = conn.execute("SELECT detail FROM crawl_log").fetchone()
    assert log["detail"].startswith("qa: checked=1 confirmed=1")


def test_cancelled_writes_claim_and_triggers_resolve(conn, monkeypatch):
    sid, eid, oid = _setup(conn)
    monkeypatch.setattr(handlers, "_qa_verify", lambda tx, occ, job_id: "cancelled")
    out = handlers.qa_check(_job(oid), conn)
    assert out == [{"kind": "resolve", "payload": {}}]

    src = conn.execute("SELECT trust FROM source WHERE id = %s", (sid,)).fetchone()
    assert abs(src["trust"] - 0.72) < 1e-9  # 0.8 * 0.9 + 0.1 * 0
    claim = conn.execute(
        """
        SELECT c.payload FROM event_claim c JOIN source s ON s.id = c.source_id
        WHERE s.url = %s
        """,
        (handlers.QA_SOURCE_URL,),
    ).fetchone()
    assert claim["payload"]["status"]["value"] == "cancelled"
    # the QA verifier's own trust is never EMA-adjusted
    qa = conn.execute(
        "SELECT trust FROM source WHERE url = %s", (handlers.QA_SOURCE_URL,)
    ).fetchone()
    assert qa["trust"] == 0.9


def test_risk_verification_contradiction_is_evidence_bearing(conn, monkeypatch):
    sid, _, oid = _setup(conn)
    verdict = handlers.RiskVerdict(
        outcome="contradicted",
        actual_starts_at="2026-04-12",
        evidence="Sonntag, 12. April 2026",
    )
    monkeypatch.setattr(handlers, "_risk_verify", lambda *a: verdict)

    out = handlers.verify_event({
        "id": uuid.uuid4(),
        "payload": {
            "occurrence_id": str(oid),
            "reason": "singleton_portal_cross_host",
        },
    }, conn)

    assert out == [{"kind": "resolve", "payload": {}}]
    claim = conn.execute(
        "SELECT c.payload, c.raw_excerpt FROM event_claim c "
        "JOIN source s ON s.id = c.source_id WHERE s.url = %s",
        (handlers.QA_SOURCE_URL,),
    ).fetchone()
    assert claim["payload"]["status"]["value"] == "cancelled"
    assert claim["raw_excerpt"] == "Sonntag, 12. April 2026"
    trust = conn.execute(
        "SELECT trust FROM source WHERE id = %s", (sid,),
    ).fetchone()["trust"]
    assert abs(trust - 0.72) < 1e-9
    log = conn.execute(
        "SELECT detail FROM crawl_log WHERE job_id IS NOT NULL"
    ).fetchone()
    assert "outcome=contradicted" in log["detail"]


def test_risk_verification_unverified_never_mutates_trust_or_claims(
    conn, monkeypatch,
):
    sid, _, oid = _setup(conn)
    monkeypatch.setattr(
        handlers, "_risk_verify",
        lambda *a: handlers.RiskVerdict(
            outcome="unverified", actual_starts_at=None, evidence=None,
        ),
    )

    assert handlers.verify_event({
        "id": uuid.uuid4(),
        "payload": {"occurrence_id": str(oid), "reason": "risk"},
    }, conn) == []

    assert conn.execute(
        "SELECT trust FROM source WHERE id = %s", (sid,),
    ).fetchone()["trust"] == 0.8
    assert conn.execute(
        "SELECT count(*) AS n FROM source WHERE url = %s",
        (handlers.QA_SOURCE_URL,),
    ).fetchone()["n"] == 0
    assert conn.execute(
        "SELECT count(*) AS n FROM event_claim c JOIN source s "
        "ON s.id = c.source_id WHERE s.url = %s",
        (handlers.QA_SOURCE_URL,),
    ).fetchone()["n"] == 0


def test_projected_occurrences_are_not_sampled(conn, monkeypatch):
    _, eid, oid = _setup(conn)
    conn.execute("UPDATE occurrence SET projected = true WHERE id = %s", (oid,))
    monkeypatch.setattr(
        handlers, "_qa_verify",
        lambda *a: (_ for _ in ()).throw(AssertionError("sampled a projection")),
    )
    handlers.qa_check({"id": uuid.uuid4(), "payload": {"sample": 5}}, conn)
    log = conn.execute("SELECT detail FROM crawl_log").fetchone()
    assert "checked=0" in log["detail"]
