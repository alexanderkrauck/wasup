#!/usr/bin/env bash
# Build and run the dev Postgres container (idempotent).
set -euo pipefail
cd "$(dirname "$0")/.."

docker build -t eventindex-db .
docker rm -f eventindex-db 2>/dev/null || true
docker run -d --name eventindex-db \
  -e POSTGRES_USER=eventindex \
  -e POSTGRES_PASSWORD=eventindex \
  -e POSTGRES_DB=eventindex \
  -p 5432:5432 \
  -v eventindex-pgdata:/var/lib/postgresql/data \
  eventindex-db

echo "waiting for postgres..."
until docker exec eventindex-db pg_isready -U eventindex -d eventindex -q; do sleep 1; done
echo "ready: postgresql://eventindex:eventindex@localhost:5432/eventindex"
