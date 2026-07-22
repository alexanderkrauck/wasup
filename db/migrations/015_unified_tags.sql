-- One confidence-bearing tag system. event_tag is rebuildable canonical
-- state; tag_embedding is a derived local-model cache shared by equal tag
-- names. The old text[]/vibe vector split was unused and is removed.

CREATE TABLE event_tag (
    event_id    uuid NOT NULL REFERENCES event(id) ON DELETE CASCADE,
    name        text NOT NULL,
    confidence  float NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    origins     text[] NOT NULL DEFAULT '{}',
    origin_confidences jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (event_id, name),
    CHECK (name = lower(name)),
    CHECK (jsonb_typeof(origin_confidences) = 'object'),
    CHECK (char_length(name) BETWEEN 1 AND 60)
);
CREATE INDEX event_tag_name_idx ON event_tag (name);

CREATE TABLE tag_embedding (
    name        text PRIMARY KEY,
    embedding   vector(768) NOT NULL,
    model       text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE event DROP COLUMN tags;
ALTER TABLE event DROP COLUMN vibe_embedding;
