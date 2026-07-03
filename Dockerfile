# Dev/deploy database image: Postgres 16 + PostGIS + pgvector.
# The app itself is not containerized (DECISIONS.md: VPS + systemd).
# pgvector base (multi-arch, incl. arm64) + PostGIS from Debian packages;
# postgis/postgis has no arm64 images.
FROM pgvector/pgvector:pg16
RUN apt-get update && apt-get install -y --no-install-recommends \
    postgresql-16-postgis-3 postgresql-16-postgis-3-scripts \
    && rm -rf /var/lib/apt/lists/*
