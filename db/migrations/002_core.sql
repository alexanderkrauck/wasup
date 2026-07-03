-- Core tables per ARCHITECTURE.md §2. Columns v1 leaves null are created now
-- (schema is the contract; inference stays off per scope fence).

CREATE TABLE venue (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name            text NOT NULL,
    aliases         text[] NOT NULL DEFAULT '{}',
    address         text,
    geo             geometry(Point, 4326),
    capacity        int,
    gmaps_place_id  text,
    kind            text
);

CREATE TABLE source (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name               text NOT NULL,
    url                text NOT NULL,
    kind               text NOT NULL,  -- website | ics | rss | api | instagram | facebook | portal | newsletter | pdf_page
    entity_type        text,           -- venue | gym | verein | church | university | promoter | portal | ...
    venue_id           uuid REFERENCES venue(id),
    geo                geometry(Point, 4326),
    tier               int NOT NULL,   -- 1=official APIs/portals, 2=structured, 3=unstructured, 4=socials/OCR
    trust              float NOT NULL,
    crawl_interval     interval NOT NULL DEFAULT '1 day',
    last_crawled       timestamptz,
    last_yield         int,
    yield_ema          float NOT NULL DEFAULT 0,
    extraction_hint    jsonb,
    discovered_via     text,           -- gmaps | link_graph | search | manual | portal_backlink
    status             text NOT NULL DEFAULT 'active',  -- active | dormant | dead | blocked
    monthly_budget_eur numeric NOT NULL DEFAULT 1.0     -- §5b cost governance, default by tier at registration
);

-- Append-only: rows are never updated or deleted (H0).
CREATE TABLE event_claim (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id     uuid NOT NULL REFERENCES source(id),
    crawl_id      uuid,
    fingerprint   text NOT NULL,
    raw_excerpt   text,
    payload       jsonb NOT NULL,  -- extracted fields, each {value, confidence}
    extracted_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX event_claim_fingerprint_idx ON event_claim (fingerprint);
CREATE INDEX event_claim_source_idx ON event_claim (source_id, extracted_at);

-- Canonical layer: rebuildable materialized view of claims (H0).
-- Only `identity` below survives rebuilds.
CREATE TABLE event (
    id                     uuid PRIMARY KEY,
    kind                   text NOT NULL,  -- one_off | series | course | festival | standing_offering
    parent_event_id        uuid REFERENCES event(id),
    title                  text NOT NULL,
    description            text,
    rights                 text,           -- quoted | generated | licensed
    category               text[] NOT NULL DEFAULT '{}',
    tags                   text[] NOT NULL DEFAULT '{}',
    venue_id               uuid REFERENCES venue(id),
    geo                    geometry(Point, 4326),
    is_recurring           bool NOT NULL DEFAULT false,
    rrule                  text,
    -- logistics
    registration_required  bool,
    registration_deadline  timestamptz,
    booking_url            text,
    drop_in_ok             bool,
    doors_at_offset        interval,
    late_entry_ok          bool,
    participation_mode     text,           -- spectate | participate | both
    price_min              numeric,
    price_max              numeric,
    url                    text,
    image_url              text,
    lang                   text,
    -- inferred attributes: v1 leaves these null (scope fence H7.2)
    expected_age_range            int4range,
    expected_age_range_confidence float,
    expected_gender_split             float,
    expected_gender_split_confidence  float,
    expected_attendance            int,
    expected_attendance_confidence float,
    expected_fullness            float,
    expected_fullness_confidence float,
    vibe_embedding         vector(1024),
    field_provenance       jsonb,
    confidence             float,
    status                 text NOT NULL DEFAULT 'confirmed',  -- confirmed | tentative | cancelled | past
    first_seen             timestamptz NOT NULL DEFAULT now(),
    last_seen              timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE occurrence (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id           uuid NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    starts_at          timestamptz NOT NULL,
    ends_at            timestamptz,
    status             text NOT NULL DEFAULT 'scheduled',  -- scheduled | cancelled | moved | postponed_tba
    availability       text,       -- available | limited | waitlist | full | NULL=unknown
    waitlist_url       text,
    fullness_estimate  float,
    last_confirmed_at  timestamptz
);
CREATE INDEX occurrence_starts_idx ON occurrence (starts_at);
CREATE INDEX occurrence_event_idx ON occurrence (event_id);

-- H0.1: fingerprint-lineage -> stable event id. Written on first sight, never rebuilt.
CREATE TABLE identity (
    fingerprint  text PRIMARY KEY,
    event_id     uuid NOT NULL,
    first_seen   timestamptz NOT NULL DEFAULT now()
);
