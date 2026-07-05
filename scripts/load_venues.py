"""Load/refresh seed venues from db/seeds/venues.csv (idempotent by name)."""

import csv

from eventindex import config, db

SEEDS = config.ROOT / "db" / "seeds" / "venues.csv"


def main() -> None:
    with db.connect() as conn, conn.transaction():
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS venue_name_key ON venue (name)")
        n = 0
        for row in csv.DictReader(SEEDS.open()):
            aliases = [a for a in row["aliases"].split("|") if a]
            conn.execute(
                """
                INSERT INTO venue (name, aliases, kind, geo)
                VALUES (%(name)s, %(aliases)s, %(kind)s,
                        ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326))
                ON CONFLICT (name) DO UPDATE SET
                    aliases = EXCLUDED.aliases, kind = EXCLUDED.kind,
                    geo = EXCLUDED.geo
                """,
                {
                    "name": row["name"],
                    "aliases": aliases,
                    "kind": row["kind"],
                    "lat": float(row["lat"]),
                    "lon": float(row["lon"]),
                },
            )
            n += 1
    print(f"loaded {n} venues")


if __name__ == "__main__":
    main()
