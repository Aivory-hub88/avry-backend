-- Schema owner: avry-backend. Also applied at startup by app/database/pg_service.py.
-- Per-account seat contract (Enterprise). NULL = plan default from
-- app/services/tiers.py SEAT_LIMITS (operational 1, business 5, enterprise 15).
-- Read by the user dashboard (lib/seats.ts) when a workspace owner invites someone.
-- Set with PATCH /api/v1/admin/users/{id}/seat-limit {"seatLimit": n | null}.

ALTER TABLE identity.user_tiers ADD COLUMN IF NOT EXISTS seat_limit INTEGER
    CHECK (seat_limit IS NULL OR seat_limit > 0);
