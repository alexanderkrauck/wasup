-- Fetch state for the content-hash early exit and conditional GET (§5).
ALTER TABLE source
    ADD COLUMN last_content_hash text,
    ADD COLUMN http_etag text,
    ADD COLUMN http_last_modified text;
