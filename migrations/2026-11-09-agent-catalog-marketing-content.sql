-- Mira (marketing_content) catalog row. The seed in app/database/pg_service.py uses
-- ON CONFLICT DO NOTHING and only runs on a fresh schema, so production needs
-- this one-off insert as well. Idempotent; safe to run twice.
-- Run:  psql ... -f migrations/2026-11-09-agent-catalog-marketing-content.sql
INSERT INTO agent_catalog (id, name, description, category, tags, status, config, created_by) VALUES
  ('marketing_content', 'Marketing & Content Agent',
   'Content calendars and channel drafts from a short brief, with tracked campaign links. It only drafts: nothing is posted, broadcast or emailed without your approval.',
   'built-in', ARRAY['Content calendar','Channel drafts','Campaign links','Competitor research'], 'published', '{}'::jsonb, 'system')
ON CONFLICT (id) DO NOTHING;
