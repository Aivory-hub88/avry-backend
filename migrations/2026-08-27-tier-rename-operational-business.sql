-- Tier rename: foundation -> operational, pro/acceleration -> business,
-- intelligence -> enterprise.
--
-- Brings identity.user_tiers.tier onto the 2026 pricing-rebrand vocabulary,
-- which is now the same string used by the marketing site's product id, the
-- price avry-payments charges, and the tier the entitlements route grants.
--
-- Safe to re-run: the UPDATE only matches pre-rebrand values, so a second run
-- reports 0 rows. Application code accepts BOTH vocabularies (see
-- app/services/tiers.py ALIASES), so it does not matter whether this runs
-- before or after the deploy, and a rollback of the code does not strand
-- migrated rows.
--
-- Historical rows are deliberately NOT touched: billing.entitlement_grants and
-- the payment/order tables record what was sold under the name it was sold
-- under, and rewriting them would falsify the billing history.

BEGIN;

-- Preview — run these first and eyeball the counts before committing.
--   SELECT tier, count(*) FROM identity.user_tiers GROUP BY tier ORDER BY 2 DESC;

UPDATE identity.user_tiers
   SET tier       = CASE lower(tier)
                      WHEN 'foundation'   THEN 'operational'
                      WHEN 'pro'          THEN 'business'
                      WHEN 'acceleration' THEN 'business'
                      WHEN 'intelligence' THEN 'enterprise'
                    END,
       updated_at = now()
 WHERE lower(tier) IN ('foundation', 'pro', 'acceleration', 'intelligence');

-- Verify — every remaining tier should be one of the canonical values, the
-- one-time grants, or 'free'.
--   SELECT tier, count(*) FROM identity.user_tiers GROUP BY tier ORDER BY 2 DESC;

COMMIT;
