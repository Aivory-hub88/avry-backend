-- Nara (deep_researcher) catalog row. The seed in app/database/pg_service.py uses
-- ON CONFLICT DO NOTHING and only runs on a fresh schema, so production needs
-- this one-off insert as well. Idempotent; safe to run twice.
-- Run:  psql ... -f migrations/2026-11-21-agent-catalog-deep-researcher.sql
INSERT INTO agent_catalog (id, name, description, category, tags, status, config, created_by) VALUES
  ('deep_researcher', 'Research & Evidence Agent',
   'Source-cited research on companies, markets, regulations and topics, with contradictions flagged and confidence stated. Read-only: it never sends messages or changes your data.',
   'built-in', ARRAY['Cited research','Competitor and market briefs','Regulation checks','Contradiction flags'], 'published', '{}'::jsonb, 'system')
ON CONFLICT (id) DO NOTHING;
