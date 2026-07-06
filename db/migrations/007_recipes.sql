-- Phase 3 (§5b): recipes live on the source; cost tracking feeds the
-- adaptive scheduler's priority = value/cost.
ALTER TABLE source
    ADD COLUMN recipe jsonb,
    ADD COLUMN recipe_version int NOT NULL DEFAULT 0,
    ADD COLUMN cost_ema numeric NOT NULL DEFAULT 0;  -- EUR per crawl, EMA
