-- Cached seriesness verdicts (red team 2026-07-21): may a series-looking
-- group be PROJECTED beyond its observed dates? Same content-cache pattern
-- as text_recurrence: one cheap LLM judgment per content key, rebuilds free.
CREATE TABLE series_judgment (
    content_key text PRIMARY KEY,
    verdict     text NOT NULL CHECK (verdict IN ('recurring', 'one_off', 'not_an_event')),
    created_at  timestamptz NOT NULL DEFAULT now()
);
