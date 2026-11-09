-- Juno (operations_inventory) catalog row. The seed in app/database/pg_service.py uses
-- ON CONFLICT DO NOTHING and only runs on a fresh schema, so production needs
-- this one-off insert as well. Idempotent; safe to run twice.
-- Run:  psql ... -f migrations/2026-11-09-agent-catalog-operations-inventory.sql
INSERT INTO agent_catalog (id, name, description, category, tags, status, config, created_by) VALUES
  ('operations_inventory', 'Operations & Inventory Agent',
   'Daily stock checks, reorder suggestions with the maths shown, and draft purchase orders. Nothing is confirmed or sent without your approval.',
   'built-in', ARRAY['Low-stock list','Reorder suggestions','Draft purchase orders','Late deliveries'], 'published', '{}'::jsonb, 'system')
ON CONFLICT (id) DO NOTHING;
