-- Phase 4 remainder: projection flag, free-text recurrence cache, user
-- reports, API keys.

-- Forward-projected occurrences (implicit series continued past the source
-- feed's horizon) are estimates and every consumer must be able to see that.
ALTER TABLE occurrence ADD COLUMN projected bool NOT NULL DEFAULT false;

-- Cache for regex-gated free-text recurrence extraction ("jeden Mittwoch
-- 3.6.-26.8." in a description). Keyed by content hash; recurrence is NULL
-- when the LLM decided the text is not actually recurring. Same
-- survive-rollback contract as `adjudication`: verdicts cost money.
CREATE TABLE text_recurrence (
    content_key text PRIMARY KEY,   -- md5(description)
    recurrence  jsonb,              -- Recurrence schema dump, or NULL
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- User feedback (§9): wrong/cancelled/duplicate -> QA queue -> source trust.
CREATE TABLE report (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    occurrence_id  uuid NOT NULL,
    reason         text NOT NULL CHECK (reason IN ('wrong', 'cancelled', 'duplicate')),
    note           text,
    created_at     timestamptz NOT NULL DEFAULT now()
);

-- API keys (§9): one table, one middleware function, no auth framework.
-- Bootstrap rule: while this table has no active row, the API is open.
CREATE TABLE api_key (
    key        text PRIMARY KEY,
    name       text NOT NULL,
    active     bool NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
