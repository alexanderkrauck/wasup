-- Audit 2026-07-12 / FIX-PLAN Block 4 (decisions 2026-07-13):
-- organizer was extracted in 7,596 claims and dropped for lack of a column;
-- time_unknown makes date-only starts honest (38% of occurrences were
-- presented as real midnights); three columns never had an extraction path.
ALTER TABLE event ADD COLUMN organizer text;
ALTER TABLE occurrence ADD COLUMN time_unknown boolean NOT NULL DEFAULT false;
ALTER TABLE event DROP COLUMN drop_in_ok;
ALTER TABLE event DROP COLUMN participation_mode;
ALTER TABLE event DROP COLUMN doors_at_offset;
