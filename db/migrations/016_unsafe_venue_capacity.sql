-- The first venue-grounding rollout could accept an LLM's same-name capacity
-- result even when Places had not corroborated the venue. It could also treat
-- postcode/locality placeholders as venue names. Remove only values proven by
-- that rollout's own log to have been written under either unsafe condition.
WITH unsafe AS (
    SELECT DISTINCT
        v.id,
        v.name ~* '^\s*\d{4,5}\s*(,\s*|\s+)[^\d,]+\s*$' AS location_only
    FROM venue v
    JOIN jobs j
      ON j.kind = 'ground_venue'
     AND j.payload->>'venue_id' = v.id::text
    JOIN crawl_log c ON c.job_id = j.id
    CROSS JOIN LATERAL regexp_match(
        c.detail, 'capacity=([0-9]+)'
    ) AS captured
    WHERE c.detail LIKE 'ground_venue:%'
      AND v.capacity = captured[1]::integer
      AND (
          c.detail LIKE 'ground_venue: matched=False %'
          OR v.name ~* '^\s*\d{4,5}\s*(,\s*|\s+)[^\d,]+\s*$'
      )
)
UPDATE venue v
SET capacity = NULL,
    gmaps_place_id = CASE
        WHEN unsafe.location_only THEN NULL
        ELSE v.gmaps_place_id
    END
FROM unsafe
WHERE v.id = unsafe.id;
