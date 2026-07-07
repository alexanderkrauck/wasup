-- The url uniqueness is load-bearing (probe/companion registration uses
-- ON CONFLICT (url)); it lived only in scripts/load_sources.py until now.
CREATE UNIQUE INDEX IF NOT EXISTS source_url_key ON source (url);
