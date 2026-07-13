-- Probe rejections had no memory: sweeps re-discovered and re-probed the
-- same rejected domains every run (sport-ooe.at fetched and judged 3x in
-- one week), crowding MAX_PROBES_PER_SWEEP, and the verdict's score and
-- concerns were discarded (H4.1 wants exactly that forensics). One row per
-- candidate domain, upserted on every rejection, cleared on registration.
CREATE TABLE probe_rejection (
    domain      text PRIMARY KEY,
    url         text NOT NULL,
    detail      text,
    score       real,
    rejected_at timestamptz NOT NULL DEFAULT now()
);
