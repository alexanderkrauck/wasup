-- One-off repair after the resolver fix (audit A1, FIX-PLAN Block 1).
-- The venue table is the only persistent state poisoned by the alias
-- snowball; canon (event/occurrence) is rebuilt from claims anyway.
-- Run once on prod, then enqueue a resolve job.
BEGIN;

-- detach canon + sources so venues can be dropped (rebuild re-links)
UPDATE event SET venue_id = NULL WHERE venue_id IS NOT NULL;
UPDATE source SET venue_id = NULL WHERE venue_id IS NOT NULL;

-- every alias was auto-grown or is re-derivable from the adjudication
-- cache (which replays on rebuild, now guarded by geo + genericness)
UPDATE venue SET aliases = '{}';

-- venues no claim names exactly only existed as snowball attractors
DELETE FROM venue v WHERE NOT EXISTS (
    SELECT 1 FROM event_claim c
    WHERE lower(c.payload->'venue_name'->>'value') = lower(v.name)
);

-- generic location strings never deserve a venue row (matches
-- venues.is_generic_location; claims re-resolve to NULL = unknown)
DELETE FROM venue WHERE lower(name) IN (
    'linz', 'linz innenstadt', 'innenstadt', 'innenstadt linz',
    'stadt linz', 'stadtgebiet linz', 'linz-urfahr', 'urfahr', 'zentrum',
    'online', 'oberösterreich', 'österreich', 'austria', 'wien',
    'linz, austria', 'linz, österreich', 'verschiedene orte', 'diverse'
);

COMMIT;
