"""Probe domain rules: which listing_url a probe may adopt (injection
defense) and when a candidate counts as known (sweep convergence).
Found live 2026-07-11: the exact-host rule discarded factory300's own
events.factory300.at listing and onboarding exhausted from the homepage."""

from eventindex.discovery.probe import domain_of, is_known, is_owned_by


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
