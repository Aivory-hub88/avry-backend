-- APPLIED TO PRODUCTION 2026-08-30. Schema owner: product (avry-backend).
-- Foundation for bring-your-own email accounts; nothing reads it yet.

-- Bring-your-own email account (SMTP send + IMAP read) for Cerveau agents.
--
-- The point of this feature is a mailbox the tenant creates *for the agent*,
-- not their primary one -- which is what makes storing a password acceptable
-- at all: the blast radius is one purpose-made box, revoked by deleting it.
--
-- Password uses the same AES-256-GCM primitive as
-- product.tenant_custom_mcp_servers.auth_header_value_encrypted
-- (app/services/mcp_server_encryption.py). Never returned by any
-- dashboard-facing route.
BEGIN;

CREATE TABLE IF NOT EXISTS product.tenant_email_accounts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id             TEXT NOT NULL,
    email_address       TEXT NOT NULL,
    from_name           TEXT,

    smtp_host           TEXT NOT NULL,
    smtp_port           INTEGER NOT NULL,
    -- 'tls' = implicit TLS on connect (465); 'starttls' = upgrade after
    -- greeting (587). Plaintext is deliberately not an option.
    smtp_security       TEXT NOT NULL DEFAULT 'starttls',

    imap_host           TEXT NOT NULL,
    imap_port           INTEGER NOT NULL DEFAULT 993,

    password_encrypted  BYTEA NOT NULL,

    status              TEXT NOT NULL DEFAULT 'pending_verification',
    -- The tenant's explicit "send as" choice. A partial unique index below
    -- makes "at most one sending address per tenant" a database guarantee
    -- rather than something the API has to remember to enforce.
    is_sender           BOOLEAN NOT NULL DEFAULT FALSE,

    last_verified_at    TIMESTAMPTZ,
    last_verify_error   TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    disabled_at         TIMESTAMPTZ,

    CONSTRAINT tenant_email_accounts_status_check
        CHECK (status IN ('pending_verification','verified','verification_failed','disabled')),
    CONSTRAINT tenant_email_accounts_smtp_security_check
        CHECK (smtp_security IN ('starttls','tls')),
    CONSTRAINT tenant_email_accounts_smtp_port_range
        CHECK (smtp_port BETWEEN 1 AND 65535),
    CONSTRAINT tenant_email_accounts_imap_port_range
        CHECK (imap_port BETWEEN 1 AND 65535),
    -- Shape only; real deliverability is proven by the verify handshake.
    CONSTRAINT tenant_email_accounts_email_shape
        CHECK (email_address ~ '^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$')
);

CREATE UNIQUE INDEX IF NOT EXISTS tenant_email_accounts_user_address_idx
    ON product.tenant_email_accounts (user_id, lower(email_address))
    WHERE disabled_at IS NULL;

-- At most one sending address per tenant. Enforced here so the "Send as"
-- radio cannot be violated by a bug in the API layer.
CREATE UNIQUE INDEX IF NOT EXISTS tenant_email_accounts_one_sender_idx
    ON product.tenant_email_accounts (user_id)
    WHERE is_sender AND disabled_at IS NULL;

COMMIT;
