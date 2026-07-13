"""VenueResolver: the production snowball (audit A1: 'OK Linz' absorbed 190
aliases and 340 events via word_similarity + alias growth) must stay dead.
Fuzzy matching works against the canonical name only, never grows aliases,
refuses geo-contradicted matches, and generic location strings resolve to
nothing rather than to something wrong."""

import uuid

from eventindex.resolve.venues import VenueResolver, is_generic_location


def _venue(conn, name, aliases=(), lat=None, lon=None):
    vid = uuid.uuid4()
    conn.execute(
        "INSERT INTO venue (id, name, aliases, geo) VALUES (%s, %s, %s, "
        "CASE WHEN %s::float IS NULL THEN NULL "
        "ELSE ST_SetSRID(ST_MakePoint(%s, %s), 4326) END)",
        (vid, name, list(aliases), lat, lon, lat),
    )
    return vid


def _aliases(conn, vid):
    return conn.execute(
        "SELECT aliases FROM venue WHERE id = %s", (vid,)
    ).fetchone()["aliases"]


def test_exact_name_and_alias_match(conn):
    vid = _venue(conn, "Posthof", aliases=["Posthof Linz"])
    r = VenueResolver(conn)
    assert r.resolve("posthof") == vid
    assert r.resolve("Posthof Linz") == vid


def test_fuzzy_symmetric_match(conn):
    vid = _venue(conn, "Posthof")
    assert VenueResolver(conn).resolve("Posthof Linz") == vid


def test_word_match_needs_distinctive_short_side(conn):
    # 'Brucknerhaus' is distinctive -> room strings still resolve to it
    vid = _venue(conn, "Brucknerhaus")
    assert VenueResolver(conn).resolve("Großer Saal, Brucknerhaus Linz") == vid


def test_poisoned_alias_no_longer_attracts(conn):
    # the exact production failure: alias 'Linz' + word_similarity = 1.0
    _venue(conn, "OK Linz", aliases=["Linz"])
    r = VenueResolver(conn)
    got = r.resolve("Pfarrkirche Linz-Christkönig")
    assert got is not None
    assert conn.execute(
        "SELECT name FROM venue WHERE id = %s", (got,)
    ).fetchone()["name"] == "Pfarrkirche Linz-Christkönig"  # new venue


def test_fuzzy_match_grows_no_alias(conn):
    vid = _venue(conn, "Posthof")
    VenueResolver(conn).resolve("Posthof Linz")
    assert _aliases(conn, vid) == []


def test_generic_strings_resolve_to_none(conn):
    r = VenueResolver(conn)
    assert is_generic_location("Linz")
    assert r.resolve("Linz") is None
    assert r.resolve("Innenstadt") is None
    assert r.resolve("Online") is None
    assert r.resolve("Weißenwolffstraße 27, Linz") is None  # address-only
    n = conn.execute("SELECT count(*) AS n FROM venue").fetchone()["n"]
    assert n == 0  # and none of them created a venue


def test_geo_veto_refuses_distant_fuzzy_match(conn):
    _venue(conn, "Stadtplatz", lat=48.30, lon=14.29)  # Linz
    r = VenueResolver(conn)
    got = r.resolve("Stadtplatz Enns", lat=48.21, lon=14.48)  # 20 km away
    assert conn.execute(
        "SELECT name FROM venue WHERE id = %s", (got,)
    ).fetchone()["name"] == "Stadtplatz Enns"


def test_geo_backfill_on_match(conn):
    vid = _venue(conn, "Posthof")  # no geo
    VenueResolver(conn).resolve("Posthof Linz", lat=48.32, lon=14.30)
    geo = conn.execute(
        "SELECT ST_Y(geo) AS lat FROM venue WHERE id = %s", (vid,)
    ).fetchone()
    assert geo["lat"] == 48.32
