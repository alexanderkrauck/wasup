"""Load/refresh the seed source registry from db/seeds/sources.csv.

Idempotent: upserts by url. Run: uv run python scripts/load_sources.py
"""

import csv

from eventindex import config, db

SEEDS = config.ROOT / "db" / "seeds" / "sources.csv"


def main() -> None:
    with db.connect() as conn, conn.transaction():
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS source_url_key ON source (url)")
        n = 0
        for row in csv.DictReader(SEEDS.open()):
            tier = int(row["tier"])
            conn.execute(
                """
                INSERT INTO source (name, url, kind, entity_type, tier, trust,
                                    monthly_budget_eur, discovered_via, geo)
                VALUES (%(name)s, %(url)s, %(kind)s, %(entity_type)s, %(tier)s,
                        %(trust)s, %(budget)s, 'manual',
                        CASE WHEN %(lat)s::float IS NULL THEN NULL
                             ELSE ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326) END)
                ON CONFLICT (url) DO UPDATE SET
                    name = EXCLUDED.name, kind = EXCLUDED.kind,
                    entity_type = EXCLUDED.entity_type, tier = EXCLUDED.tier,
                    geo = EXCLUDED.geo
                """,
                {
                    "name": row["name"],
                    "url": row["url"],
                    "kind": row["kind"],
                    "entity_type": row["entity_type"],
                    "tier": tier,
                    "trust": float(row["trust"]),
                    "budget": config.MONTHLY_BUDGET_EUR_BY_TIER.get(tier, 1.0),
                    "lat": float(row["lat"]) if row["lat"] else None,
                    "lon": float(row["lon"]) if row["lon"] else None,
                },
            )
            n += 1
    print(f"loaded {n} sources")


if __name__ == "__main__":
    main()
