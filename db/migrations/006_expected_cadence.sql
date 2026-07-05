-- §7 staleness decay: each event carries its expected re-confirmation
-- cadence (min crawl_interval of its sources, set at rebuild). The decay
-- itself is computed at query time in the API - never a batch job.
ALTER TABLE event ADD COLUMN expected_cadence interval;
