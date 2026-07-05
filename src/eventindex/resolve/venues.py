"""Venue canonicalization (H2.1): venue-first resolution.

Match order: exact name/alias -> trigram similarity (name and aliases) ->
create new. Matches grow the alias list aggressively (§6); every venue
created from a claim string lands in the weekly review dump.

similarity() alone under-scores "Großer Saal, Brucknerhaus Linz" vs
"Brucknerhaus", so the score is greatest(similarity, word_similarity).
"""

import uuid
from dataclasses import dataclass, field

MATCH_THRESHOLD = 0.55


@dataclass
class VenueResolver:
    tx: object
    created: list[str] = field(default_factory=list)  # for the review dump
    _cache: dict[str, uuid.UUID] = field(default_factory=dict)

    def resolve(self, name: str, lat=None, lon=None) -> uuid.UUID:
        key = name.strip().lower()
        if key in self._cache:
            return self._cache[key]
        venue_id = self._lookup(name) or self._create(name, lat, lon)
        self._cache[key] = venue_id
        return venue_id

    def _lookup(self, name: str) -> uuid.UUID | None:
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
            SELECT id, greatest(
                similarity(name, %(n)s),
                word_similarity(name, %(n)s),
                coalesce((SELECT max(greatest(similarity(a, %(n)s), word_similarity(a, %(n)s)))
                          FROM unnest(aliases) a), 0)
            ) AS sim
            FROM venue ORDER BY sim DESC LIMIT 1
            """,
            {"n": name},
        ).fetchone()
        if best and best["sim"] >= MATCH_THRESHOLD:
            # aggressive alias growth: remember this spelling
            self.tx.execute(
                "UPDATE venue SET aliases = array_append(aliases, %(n)s) "
                "WHERE id = %(id)s AND NOT (%(n)s = ANY(aliases)) AND lower(name) != lower(%(n)s)",
                {"n": name, "id": best["id"]},
            )
            return best["id"]
        return None

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
