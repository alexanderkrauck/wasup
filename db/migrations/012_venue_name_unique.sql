-- Codify prod reality (schema drift found 2026-07-13): venue_name_key was
-- created manually on prod but never migrated; VenueResolver._create's
-- ON CONFLICT (name) depends on it. IF NOT EXISTS keeps prod idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS venue_name_key ON venue (name);
CREATE INDEX IF NOT EXISTS venue_name_trgm_idx ON venue USING gin (name gin_trgm_ops);
