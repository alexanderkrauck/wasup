-- Atomic paid-provider reservations and one durable provider circuit.
-- A reservation is counted before the external request starts, then settled
-- to the provider-reported/estimated actual amount.  Process crashes therefore
-- consume allowance for the day instead of permitting an unbounded overshoot.
ALTER TABLE budget_spend
    ADD COLUMN reserved_eur numeric NOT NULL DEFAULT 0,
    ADD COLUMN state text NOT NULL DEFAULT 'settled',
    ADD COLUMN lane text NOT NULL DEFAULT 'core',
    ADD COLUMN provider text;

ALTER TABLE budget_spend
    ADD CONSTRAINT budget_spend_nonnegative
        CHECK (amount_eur >= 0 AND reserved_eur >= 0),
    ADD CONSTRAINT budget_spend_state_valid
        CHECK (state IN ('reserved', 'settled', 'uncertain')),
    ADD CONSTRAINT budget_spend_lane_valid
        CHECK (lane IN ('core', 'recovery', 'interactive')),
    ADD CONSTRAINT budget_spend_reservation_shape
        CHECK (
            (state = 'reserved' AND amount_eur = 0 AND reserved_eur > 0)
            OR (state IN ('settled', 'uncertain') AND reserved_eur = 0)
        );

CREATE INDEX budget_spend_lane_day_idx
    ON budget_spend (lane, spent_at);

-- Existing rows predate lane attribution.  Recover it from their durable job
-- ids so historical digests do not pretend bulk recovery was core collection.
UPDATE budget_spend b
SET lane = 'recovery'
FROM jobs j
WHERE b.job_id = j.id
  AND j.kind IN (
      'enrich', 'ground_venue', 'hydrate_event', 'timefix', 'verify_event'
  );

CREATE TABLE provider_circuit (
    provider       text PRIMARY KEY,
    blocked_until  timestamptz NOT NULL,
    reason         text NOT NULL,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
