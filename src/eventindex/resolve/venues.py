"""Venue canonicalization (H2.1): venue-first resolution.

Match order: exact name/alias -> trigram similarity against the canonical
NAME -> create new. Aliases are never grown from fuzzy matches: that
snowballed in production (generic strings like "Linz" landed in an alias
list, word_similarity('Linz', anything containing Linz) = 1.0, and one
venue absorbed 190 spellings and 340 events). Aliases come only from
adjudicated venue merges (rebuild._reconcile_venues) and manual review.

Guards, in order:
- generic location strings (bare city/district words, plain street
  addresses) never match, never create venues, never become aliases;
- word_similarity() is asymmetric, so a fuzzy hit needs either the
  symmetric similarity() over threshold or a distinctive short side;
- a fuzzy hit whose venue geo contradicts the claim's own coordinates
  (> GEO_VETO_M) is refused;
- a match from a geo-carrying claim backfills a venue without geo.
"""

import re
import uuid
from dataclasses import dataclass, field

MATCH_THRESHOLD = 0.55
GEO_VETO_M = 300  # same tolerance as rebuild.VENUE_ALIAS_MAX_M

# strings that describe "somewhere in town", not a venue (lowercased)
_GENERIC = {
    "linz", "linz innenstadt", "innenstadt", "innenstadt linz", "stadt linz",
    "stadtgebiet linz", "linz-urfahr", "urfahr", "zentrum", "online",
    "oberösterreich", "österreich", "austria", "wien", "linz, austria",
    "linz, österreich", "verschiedene orte", "diverse", "tba", "t.b.a.",
}
# a string that is essentially just a street address
_ADDRESS_ONLY_RE = re.compile(
    r"^\s*[\w.\-/ ]{0,40}?(straße|strasse|gasse|weg|platz|ring|allee)"
    r"\s+\d+[a-z]?\b", re.IGNORECASE
)
_STOPTOKENS = {"linz", "wien", "der", "die", "das", "und", "am", "im", "an"}


def is_generic_location(name: str) -> bool:
    low = " ".join(name.strip().lower().split())
    return low in _GENERIC or bool(_ADDRESS_ONLY_RE.match(low))


def _distinctive(name: str) -> bool:
    """Is this string specific enough to anchor an asymmetric word match?
    'Brucknerhaus' yes; 'Linz' / 'Saal 3' no."""
    tokens = [t for t in re.findall(r"[\wäöüß]+", name.lower())
              if len(t) >= 4 and t not in _STOPTOKENS]
    return len(tokens) >= 2 or (len(tokens) == 1 and len(tokens[0]) >= 6)


@dataclass
class VenueResolver:
    tx: object
    created: list[str] = field(default_factory=list)  # for the review dump
    _cache: dict[str, uuid.UUID | None] = field(default_factory=dict)

    def resolve(self, name: str, lat=None, lon=None) -> uuid.UUID | None:
        key = name.strip().lower()
        if key in self._cache:
            return self._cache[key]
        if is_generic_location(name):
            venue_id = None  # unknown location beats a wrong one
        else:
            venue_id = self._lookup(name, lat, lon) or self._create(name, lat, lon)
            if lat is not None and venue_id is not None:
                self._backfill_geo(venue_id, lat, lon)
        self._cache[key] = venue_id
        return venue_id

    def _lookup(self, name: str, lat, lon) -> uuid.UUID | None:
        exact = self.tx.execute(
            """
            SELECT id FROM venue
            WHERE lower(name) = lower(%(n)s)
               OR EXISTS (SELECT 1 FROM unnest(aliases) a WHERE lower(a) = lower(%(n)s))
            LIMIT 1
            """,
            {"n": name},
        ).fetchone()
        if exact:
            return exact["id"]

        best = self.tx.execute(
            """
            SELECT id, name,
                   similarity(name, %(n)s) AS sym,
                   greatest(word_similarity(name, %(n)s),
                            word_similarity(%(n)s, name)) AS wsim,
                   CASE WHEN geo IS NULL OR %(lat)s::float IS NULL THEN NULL
                        ELSE ST_DistanceSphere(
                            geo, ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326))
                   END AS dist_m
            FROM venue
            ORDER BY greatest(similarity(name, %(n)s),
                              word_similarity(name, %(n)s),
                              word_similarity(%(n)s, name)) DESC
            LIMIT 1
            """,
            {"n": name, "lat": lat, "lon": lon},
        ).fetchone()
        if not best:
            return None
        shorter = min(name.strip(), best["name"], key=len)
        ok = best["sym"] >= MATCH_THRESHOLD or (
            best["wsim"] >= MATCH_THRESHOLD and _distinctive(shorter)
        )
        if ok and (best["dist_m"] is None or best["dist_m"] <= GEO_VETO_M):
            return best["id"]  # no alias growth here - see module docstring
        return None

    def _backfill_geo(self, venue_id: uuid.UUID, lat, lon) -> None:
        self.tx.execute(
            "UPDATE venue SET geo = ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) "
            "WHERE id = %(id)s AND geo IS NULL",
            {"id": venue_id, "lat": lat, "lon": lon},
        )

    def _create(self, name: str, lat, lon) -> uuid.UUID:
        venue_id = uuid.uuid4()
        self.tx.execute(
            """
            INSERT INTO venue (id, name, geo)
            VALUES (%(id)s, %(name)s,
                    CASE WHEN %(lat)s::float IS NULL THEN NULL
                         ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END)
            """,
            {"id": venue_id, "name": name.strip(), "lat": lat, "lon": lon},
        )
        self.created.append(name.strip())
        return venue_id
