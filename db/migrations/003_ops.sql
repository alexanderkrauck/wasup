-- Job queue (H7): the only queue in the system. Cron inserts, worker consumes
-- with SELECT ... FOR UPDATE SKIP LOCKED.
CREATE TABLE jobs (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text NOT NULL,   -- crawl | onboard | probe | resolve | enrich | qa_check | discover
    payload     jsonb NOT NULL DEFAULT '{}',
    run_after   timestamptz NOT NULL DEFAULT now(),
    budget_ctx  jsonb,
    attempts    int NOT NULL DEFAULT 0,
    status      text NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
    created_at  timestamptz NOT NULL DEFAULT now(),
    started_at  timestamptz,
    finished_at timestamptz,
    last_error  text
);
CREATE INDEX jobs_claim_idx ON jobs (status, run_after);

-- Observability (H7.3): one log table + nightly digest, no ops stack.
CREATE TABLE crawl_log (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id        uuid,
    source_id     uuid REFERENCES source(id),
    started_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz,
    status        text NOT NULL,  -- ok | unchanged | error
    events_found  int NOT NULL DEFAULT 0,
    detail        text
);
CREATE INDEX crawl_log_started_idx ON crawl_log (started_at);

-- Budget ledger (§5b): every LLM/scrape spend is a row; caps are enforced by
-- summing this table. Amounts in EUR.
CREATE TABLE budget_spend (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    spent_at    timestamptz NOT NULL DEFAULT now(),
    amount_eur  numeric NOT NULL,
    category    text NOT NULL,   -- llm | scrape | other
    source_id   uuid REFERENCES source(id),
    job_id      uuid,
    model       text,
    tokens_in   int,
    tokens_out  int,
    detail      text
);
CREATE INDEX budget_spend_spent_idx ON budget_spend (spent_at);
CREATE INDEX budget_spend_source_idx ON budget_spend (source_id, spent_at);
