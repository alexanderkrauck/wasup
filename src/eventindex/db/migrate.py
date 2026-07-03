"""Apply db/migrations/*.sql in filename order, tracked in schema_migrations."""

import sys

from eventindex import config, db


def migrate(conn) -> list[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(filename text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    applied = {
        r["filename"] for r in conn.execute("SELECT filename FROM schema_migrations")
    }
    newly_applied = []
    for path in sorted(config.MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        with conn.transaction():
            conn.execute(path.read_text())
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
            )
        newly_applied.append(path.name)
    return newly_applied


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else None
    with db.connect(url) as conn:
        for name in migrate(conn):
            print(f"applied {name}")
    print("migrations up to date")


if __name__ == "__main__":
    main()
