-- Postcode/locality placeholders are location evidence, not named venues.
-- Clear only Place IDs written for rows that have venue-grounding provenance;
-- keep their useful address/geo evidence and all independently supplied data.
UPDATE venue v
SET gmaps_place_id = NULL
WHERE v.name ~* '^\s*\d{4,5}\s*(,\s*|\s+)[^\d,]+\s*$'
  AND v.gmaps_place_id IS NOT NULL
  AND EXISTS (
      SELECT 1
      FROM jobs j
      WHERE j.kind = 'ground_venue'
        AND j.payload->>'venue_id' = v.id::text
  );
