-- Enrichment (§8 / H5): priors are ranking features, not facts. Hand-seeded,
-- editable, overridden only by explicit textual evidence.

CREATE TABLE category_priors (
    category text PRIMARY KEY,
    priors   jsonb NOT NULL  -- {age_min, age_max, gender_split, kid_friendly, ...}
);

INSERT INTO category_priors (category, priors) VALUES
('music',      '{"age_min": 18, "age_max": 50, "gender_split": 0.5,  "kid_friendly": false, "energy": "medium"}'),
('nightlife',  '{"age_min": 18, "age_max": 32, "gender_split": 0.5,  "kid_friendly": false, "energy": "high"}'),
('theatre',    '{"age_min": 30, "age_max": 70, "gender_split": 0.55, "kid_friendly": false, "energy": "low"}'),
('film',       '{"age_min": 16, "age_max": 60, "gender_split": 0.5,  "kid_friendly": false, "energy": "low"}'),
('art',        '{"age_min": 25, "age_max": 65, "gender_split": 0.55, "kid_friendly": false, "energy": "low"}'),
('culture',    '{"age_min": 25, "age_max": 70, "gender_split": 0.55, "kid_friendly": false, "energy": "low"}'),
('sport',      '{"age_min": 15, "age_max": 50, "gender_split": 0.45, "kid_friendly": false, "energy": "high"}'),
('community',  '{"age_min": 25, "age_max": 70, "gender_split": 0.5,  "kid_friendly": true,  "energy": "medium"}'),
('learning',   '{"age_min": 20, "age_max": 60, "gender_split": 0.6,  "kid_friendly": false, "energy": "low"}'),
('family',     '{"age_min": 3,  "age_max": 45, "gender_split": 0.55, "kid_friendly": true,  "energy": "medium"}'),
('market',     '{"age_min": 20, "age_max": 75, "gender_split": 0.55, "kid_friendly": true,  "energy": "low"}'),
('food_drink', '{"age_min": 20, "age_max": 60, "gender_split": 0.5,  "kid_friendly": false, "energy": "medium"}'),
('tech',       '{"age_min": 18, "age_max": 45, "gender_split": 0.35, "kid_friendly": false, "energy": "medium"}'),
('religion',   '{"age_min": 30, "age_max": 80, "gender_split": 0.55, "kid_friendly": true,  "energy": "low"}'),
('other',      '{}');

-- LLM enrichment cached by content hash: rebuilding canon must never re-pay.
CREATE TABLE enrichment (
    content_key text PRIMARY KEY,  -- md5(title|description|category|venue)
    attributes  jsonb NOT NULL,    -- {attr: {value, confidence, evidence?}}
    model       text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- Extended inferred attributes beyond the typed §2 columns:
-- {language, kid_friendly, newcomer_friendly, outdoor, energy, ...}
ALTER TABLE event ADD COLUMN inferred jsonb;
