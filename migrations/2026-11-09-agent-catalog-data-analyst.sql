-- Ivo (data_analyst) catalog row. The seed in app/database/pg_service.py uses
-- ON CONFLICT DO NOTHING and only runs on a fresh schema, so production needs
-- this one-off insert as well. Idempotent; safe to run twice.
-- Run:  psql ... -f migrations/2026-11-09-agent-catalog-data-analyst.sql
INSERT INTO agent_catalog (id, name, description, category, tags, status, config, created_by) VALUES
  ('data_analyst', 'Data & Reporting Analyst',
   'Weekly business reports, KPI answers with their sources, sales forecasts and early warnings. Read-only: it never changes your data.',
   'built-in', ARRAY['Weekly report','KPIs with sources','Forecast','Anomaly flags'], 'published', '{}'::jsonb, 'system')
ON CONFLICT (id) DO NOTHING;
