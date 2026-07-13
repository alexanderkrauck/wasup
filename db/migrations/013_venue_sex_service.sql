-- Curated venue fact (Alexander 2026-07-13): events at a commercial sex
-- establishment must ALWAYS carry sex_service_context, however innocuous
-- the event text ("Football Lounge Nights" says nothing, the venue says
-- everything - the enrichment LLM cannot be trusted to know every Etablissement).
-- null = unknown/not flagged; curation happens in chat, no UI.
ALTER TABLE venue ADD COLUMN sex_service boolean;
UPDATE venue SET sex_service = true WHERE name = 'Villa Ostende';
