"""Resolver v0 (phase 1): fingerprint-identical claims collapse into one
event; naive canon projection (freshest claim wins whole). Cross-source
weighted merging, venue resolution, and the full rebuild arrive in phase 2.

Stable ids via the identity table (H0.1): fingerprint -> event uuid, minted on
first sight, never rebuilt.
"""

import uuid

from psycopg.types.json import Jsonb

from eventindex.extract import parse_dt


def _event_id_for(tx, fp: str) -> uuid.UUID:
    row = tx.execute(
        "SELECT event_id FROM identity WHERE fingerprint = %s", (fp,)
    ).fetchone()
    if row:
        return row["event_id"]
    event_id = uuid.uuid4()
    tx.execute(
        "INSERT INTO identity (fingerprint, event_id) VALUES (%s, %s)",
        (fp, event_id),
    )
    return event_id


def _value(payload: dict, key: str):
    entry = payload.get(key)
    return entry["value"] if entry else None


def project_claim(tx, claim: dict, source: dict) -> uuid.UUID | None:
    """Upsert the canonical event + its occurrence from one claim."""
    payload = claim["payload"]
    title = _value(payload, "title")
    starts_at = parse_dt(_value(payload, "starts_at"))
    if not title or starts_at is None:
        return None

    event_id = _event_id_for(tx, claim["fingerprint"])
    lat = _value(payload, "lat")
    lon = _value(payload, "lon")
    if lat is None and source.get("lat") is not None:
        lat, lon = source["lat"], source["lon"]  # claim had no geo: source's

    confidences = [f["confidence"] for f in payload.values()]
    confidence = source["trust"] * (sum(confidences) / len(confidences))
    provenance = {
        key: {"claim": str(claim["id"]), "confidence": entry["confidence"]}
        for key, entry in payload.items()
    }
    category = _value(payload, "category")

    tx.execute(
        """
        INSERT INTO event (id, kind, title, description, rights, category,
                           geo, price_min, price_max, url, image_url,
                           field_provenance, confidence, status)
        VALUES (%(id)s, 'one_off', %(title)s, %(description)s, 'quoted',
                %(category)s,
                CASE WHEN %(lat)s::float IS NULL THEN NULL
                     ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END,
                %(price_min)s, %(price_max)s, %(url)s, %(image_url)s,
                %(provenance)s, %(confidence)s, 'confirmed')
        ON CONFLICT (id) DO UPDATE SET
            title = EXCLUDED.title,
            description = EXCLUDED.description,
            category = EXCLUDED.category,
            geo = EXCLUDED.geo,
            price_min = EXCLUDED.price_min,
            price_max = EXCLUDED.price_max,
            url = EXCLUDED.url,
            image_url = EXCLUDED.image_url,
            field_provenance = EXCLUDED.field_provenance,
            confidence = EXCLUDED.confidence,
            last_seen = now(),
            updated_at = now()
        """,
        {
            "id": event_id,
            "title": title,
            "description": _value(payload, "description"),
            "category": [category] if category else [],
            "lat": lat,
            "lon": lon,
            "price_min": _value(payload, "price_min"),
            "price_max": _value(payload, "price_max"),
            "url": _value(payload, "url") or source["url"],
            "image_url": _value(payload, "image_url"),
            "provenance": Jsonb(provenance),
            "confidence": confidence,
        },
    )

    # one_off: exactly one occurrence, refreshed in place
    ends_at = parse_dt(_value(payload, "ends_at"))
    updated = tx.execute(
        "UPDATE occurrence SET starts_at = %s, ends_at = %s, "
        "last_confirmed_at = now() WHERE event_id = %s",
        (starts_at, ends_at, event_id),
    )
    if updated.rowcount == 0:
        tx.execute(
            "INSERT INTO occurrence (event_id, starts_at, ends_at, last_confirmed_at) "
            "VALUES (%s, %s, %s, now())",
            (event_id, starts_at, ends_at),
        )
    return event_id


def resolve_crawl(tx, crawl_id) -> int:
    """Project every claim of one crawl into canon. Returns events touched."""
    claims = tx.execute(
        """
        SELECT c.*, s.trust, s.url AS source_url,
               ST_Y(s.geo) AS s_lat, ST_X(s.geo) AS s_lon
        FROM event_claim c JOIN source s ON s.id = c.source_id
        WHERE c.crawl_id = %s
        ORDER BY c.extracted_at
        """,
        (crawl_id,),
    ).fetchall()
    touched = 0
    for claim in claims:
        source = {
            "trust": claim["trust"],
            "url": claim["source_url"],
            "lat": claim["s_lat"],
            "lon": claim["s_lon"],
        }
        if project_claim(tx, claim, source) is not None:
            touched += 1
    return touched
