-- APPLIED TO PRODUCTION 2026-08-30. Schema owner: aivory_ops (the native
-- bridge domain), recorded here because this is the only migrations directory
-- in the repo -- see docs/CERVEAU-STATUS.md, "Sales & Leads Agent, Phase 1".

-- Phase 1 of the Sales & Leads Agent: give a lead a deal value.
--
-- `leads` already had `won`/`lost` stages but nothing recording what was won.
-- Currency is per-row, matching aivory_ops.invoices (currency TEXT NOT NULL
-- DEFAULT 'USD') -- the quote this agent produces hands off to that table's
-- create_invoice, and a per-tenant currency here would have forced that
-- handoff to invent one.
--
-- Deliberately NO default on `currency`: the agent must ask which currency a
-- deal is in rather than silently recording USD for an IDR business. The
-- paired CHECK below enforces that an amount cannot exist without one.
BEGIN;

ALTER TABLE aivory_ops.leads
  ADD COLUMN IF NOT EXISTS amount              NUMERIC(14,2),
  ADD COLUMN IF NOT EXISTS currency            TEXT,
  ADD COLUMN IF NOT EXISTS expected_close_date DATE,
  ADD COLUMN IF NOT EXISTS owner               TEXT,
  ADD COLUMN IF NOT EXISTS probability         SMALLINT;

ALTER TABLE aivory_ops.leads
  DROP CONSTRAINT IF EXISTS leads_amount_currency_together,
  DROP CONSTRAINT IF EXISTS leads_currency_iso,
  DROP CONSTRAINT IF EXISTS leads_probability_range,
  DROP CONSTRAINT IF EXISTS leads_amount_nonneg;

ALTER TABLE aivory_ops.leads
  -- An amount with no currency is not a number anyone can act on.
  ADD CONSTRAINT leads_amount_currency_together
    CHECK ((amount IS NULL) = (currency IS NULL)),
  -- ISO-4217 shape only: stops the model writing 'Rp', 'Rupiah' or 'usd'.
  ADD CONSTRAINT leads_currency_iso
    CHECK (currency IS NULL OR currency ~ '^[A-Z]{3}$'),
  ADD CONSTRAINT leads_probability_range
    CHECK (probability IS NULL OR (probability BETWEEN 0 AND 100)),
  ADD CONSTRAINT leads_amount_nonneg
    CHECK (amount IS NULL OR amount >= 0);

-- pipeline_summary groups by (tenant_id, stage, currency); the existing index
-- is on tenant_id alone.
CREATE INDEX IF NOT EXISTS idx_leads_tenant_stage
  ON aivory_ops.leads (tenant_id, stage);

COMMIT;
