"""Probe domain rules: which listing_url a probe may adopt (injection
defense) and when a candidate counts as known (sweep convergence).
Found live 2026-07-11: the exact-host rule discarded factory300's own
events.factory300.at listing and onboarding exhausted from the homepage.
Rejection memory added 2026-07-13: rejected domains were re-probed every
sweep (sport-ooe.at judged 3x in one week) and verdicts were discarded."""

from eventindex.discovery import probe, sweep
from eventindex.discovery.probe import (
    ProbeVerdict, domain_of, is_known, is_owned_by, probe_url,
)


def test_own_subdomain_listing_is_adopted():
    assert is_owned_by("events.factory300.at", "factory300.at")
    assert is_owned_by("factory300.at", "factory300.at")


def test_third_party_and_sibling_listings_stay_rejected():
    # page text is untrusted: never register an off-site suggestion
    assert not is_owned_by("evil.com", "factory300.at")
    # suffix without a dot boundary is a different domain
    assert not is_owned_by("notfactory300.at", "factory300.at")
    # sibling tenants on shared platforms are NOT the probed site
    assert not is_owned_by("verein-b.jimdofree.com", "verein-a.jimdofree.com")
    # a subdomain probe may not adopt its apex (could be the platform itself)
    assert not is_owned_by("jimdofree.com", "verein-a.jimdofree.com")


def test_apex_is_known_once_a_subdomain_source_exists():
    known = {"events.factory300.at", "posthof.at"}
    assert is_known("factory300.at", known)  # convergence: no eternal re-probe
    assert is_known("posthof.at", known)
    assert not is_known("kapu.at", known)
    # a sibling tenant is NOT blocked by another tenant's registration
    assert not is_known("verein-b.jimdofree.com", {"verein-a.jimdofree.com"})


def test_domain_of_strips_www_only():
    assert domain_of("https://www.factory300.at/x") == "factory300.at"
    assert domain_of("https://events.factory300.at/list") == "events.factory300.at"


class _FakeResp:
    url = "https://www.wko.at/ooe/veranstaltungen"
    content = b"<html><body>" + b"Veranstaltungen in Linz und Umgebung. " * 5 + b"</body></html>"

    def raise_for_status(self):
        pass


def _verdict(linz_area: bool, score: float) -> ProbeVerdict:
    return ProbeVerdict(
        emits_events=True, linz_area=linz_area, score=score,
        suggested_name="WKO OÖ Veranstaltungen", listing_url=None,
        entity_type="portal", concerns=["regional_mixed_locality"],
    )


def test_rejection_is_recorded_and_cleared_on_registration(conn, monkeypatch):
    monkeypatch.setattr(probe.time, "sleep", lambda s: None)
    monkeypatch.setattr(probe.httpx, "get", lambda *a, **k: _FakeResp())
    url = "https://www.wko.at/ooe/veranstaltungen"

    # linz_area=false clamps below the register bar -> remembered rejection
    monkeypatch.setattr(probe.llm, "complete",
                        lambda *a, **k: _verdict(linz_area=False, score=0.9))
    out = probe_url(conn, url, "test")
    assert out["outcome"] == "rejected"
    assert "score=0.40" in out["detail"]  # verdict survives for H4.1 forensics
    row = conn.execute("SELECT * FROM probe_rejection").fetchone()
    assert row["domain"] == "wko.at" and row["score"] == 0.4
    assert probe.recently_rejected_domains(conn) == {"wko.at"}

    # a later accepting probe registers AND clears the stale rejection
    monkeypatch.setattr(probe.llm, "complete",
                        lambda *a, **k: _verdict(linz_area=True, score=0.8))
    out = probe_url(conn, url, "test")
    assert out["outcome"] == "registered"
    assert conn.execute("SELECT * FROM probe_rejection").fetchone() is None


def test_sweep_skips_recently_rejected_domains_until_ttl(conn, monkeypatch):
    probe._reject(conn, "https://www.sport-ooe.at/olympiazentrum.htm", "junk", 0.2)
    monkeypatch.setitem(
        sweep.CHANNELS, "osm",
        lambda tx, job_id=None: ["https://sport-ooe.at/x", "https://kapu.at/"],
    )

    seen, enqueued = sweep.discover(conn, "osm")
    assert (seen, enqueued) == (2, 1)
    urls = {r["url"] for r in conn.execute(
        "SELECT payload->>'url' AS url FROM jobs WHERE kind = 'probe'")}
    assert urls == {"https://kapu.at/"}  # rejected domain not re-probed

    # TTL expiry re-heals: e.g. after a classifier fix the domain re-enters
    conn.execute("UPDATE probe_rejection SET rejected_at = now() - interval '91 days'")
    seen, enqueued = sweep.discover(conn, "osm")
    assert (seen, enqueued) == (2, 1)  # kapu now pending-deduped, sport-ooe in
    urls = {r["url"] for r in conn.execute(
        "SELECT payload->>'url' AS url FROM jobs WHERE kind = 'probe'")}
    assert "https://sport-ooe.at/x" in urls
