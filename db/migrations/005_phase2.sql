-- Phase 2: holidays (H1.2), adjudication cache/log (H2.2), venue trigram index.

-- Static holiday/Ferien table for recurrence exceptions ("außer Ferien").
-- Update once a year. Ranges inclusive. Verified against bmb.gv.at 2026/27.
CREATE TABLE holiday (
    kind      text NOT NULL,  -- public_holiday | school_holiday
    name      text NOT NULL,
    starts_on date NOT NULL,
    ends_on   date NOT NULL,
    PRIMARY KEY (kind, starts_on)
);

INSERT INTO holiday (kind, name, starts_on, ends_on) VALUES
-- national public holidays 2026
('public_holiday', 'Neujahr',              '2026-01-01', '2026-01-01'),
('public_holiday', 'Heilige Drei Könige',  '2026-01-06', '2026-01-06'),
('public_holiday', 'Ostermontag',          '2026-04-06', '2026-04-06'),
('public_holiday', 'Staatsfeiertag',       '2026-05-01', '2026-05-01'),
('public_holiday', 'Christi Himmelfahrt',  '2026-05-14', '2026-05-14'),
('public_holiday', 'Pfingstmontag',        '2026-05-25', '2026-05-25'),
('public_holiday', 'Fronleichnam',         '2026-06-04', '2026-06-04'),
('public_holiday', 'Mariä Himmelfahrt',    '2026-08-15', '2026-08-15'),
('public_holiday', 'Nationalfeiertag',     '2026-10-26', '2026-10-26'),
('public_holiday', 'Allerheiligen',        '2026-11-01', '2026-11-01'),
('public_holiday', 'Mariä Empfängnis',     '2026-12-08', '2026-12-08'),
('public_holiday', 'Christtag',            '2026-12-25', '2026-12-25'),
('public_holiday', 'Stefanitag',           '2026-12-26', '2026-12-26'),
-- national public holidays 2027
('public_holiday', 'Neujahr',              '2027-01-01', '2027-01-01'),
('public_holiday', 'Heilige Drei Könige',  '2027-01-06', '2027-01-06'),
('public_holiday', 'Ostermontag',          '2027-03-29', '2027-03-29'),
('public_holiday', 'Staatsfeiertag',       '2027-05-01', '2027-05-01'),
('public_holiday', 'Christi Himmelfahrt',  '2027-05-06', '2027-05-06'),
('public_holiday', 'Pfingstmontag',        '2027-05-17', '2027-05-17'),
('public_holiday', 'Fronleichnam',         '2027-05-27', '2027-05-27'),
('public_holiday', 'Mariä Himmelfahrt',    '2027-08-15', '2027-08-15'),
('public_holiday', 'Nationalfeiertag',     '2027-10-26', '2027-10-26'),
('public_holiday', 'Allerheiligen',        '2027-11-01', '2027-11-01'),
('public_holiday', 'Mariä Empfängnis',     '2027-12-08', '2027-12-08'),
('public_holiday', 'Christtag',            '2027-12-25', '2027-12-25'),
('public_holiday', 'Stefanitag',           '2027-12-26', '2027-12-26'),
-- OÖ school holidays 2025/26 tail + 2026/27 (bmb.gv.at)
('school_holiday', 'Sommerferien 2026',    '2026-07-11', '2026-09-13'),
('school_holiday', 'Herbstferien 2026',    '2026-10-27', '2026-10-31'),
('school_holiday', 'Allerseelen 2026',     '2026-11-02', '2026-11-02'),
('school_holiday', 'Weihnachtsferien 26/27','2026-12-24', '2027-01-06'),
('school_holiday', 'Semesterferien OÖ 2027','2027-02-15', '2027-02-20'),
('school_holiday', 'Osterferien 2027',     '2027-03-20', '2027-03-29'),
('school_holiday', 'Florianitag OÖ 2027',  '2027-05-04', '2027-05-04'),
('school_holiday', 'Pfingstferien 2027',   '2027-05-15', '2027-05-17'),
('school_holiday', 'Sommerferien 2027',    '2027-07-10', '2027-09-12');

-- LLM grey-zone merge verdicts: cache (rebuilds must not re-pay) and the
-- feedstock for gold-set growth (H2.2).
CREATE TABLE adjudication (
    pair_key      text PRIMARY KEY,  -- md5 of the sorted fingerprint pair
    fingerprint_a text NOT NULL,
    fingerprint_b text NOT NULL,
    title_a       text,
    title_b       text,
    score         float NOT NULL,
    same_event    bool NOT NULL,
    decided_by    text NOT NULL,     -- llm | gold
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX venue_name_trgm_idx ON venue USING gin (name gin_trgm_ops);
