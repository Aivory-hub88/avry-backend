-- Migration: Impersonation Sessions Schema
-- Adds the impersonation_sessions table and impersonation_permission column to users
-- Requirements: 3.5, 10.5

-- ============================================================================
-- AUDIT LOGS TABLE (required for impersonation event logging)
-- ============================================================================
CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    user_id TEXT REFERENCES users(id),
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id TEXT,
    changes JSONB,
    ip_address VARCHAR(45),
    user_agent TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_logs_user_id ON audit_logs(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_action ON audit_logs(action);
CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at);

-- ============================================================================
-- IMPERSONATION SESSIONS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS impersonation_sessions (
    id TEXT PRIMARY KEY,
    admin_user_id TEXT NOT NULL REFERENCES users(id),
    target_user_id TEXT NOT NULL REFERENCES users(id),
    access_mode VARCHAR(20) NOT NULL DEFAULT 'read_only',
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ,
    termination_reason VARCHAR(50),
    total_requests INTEGER NOT NULL DEFAULT 0,
    mutations_attempted INTEGER NOT NULL DEFAULT 0,
    mutations_blocked INTEGER NOT NULL DEFAULT 0,
    pages_visited JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_imp_sessions_admin ON impersonation_sessions(admin_user_id);
CREATE INDEX idx_imp_sessions_status ON impersonation_sessions(status);
CREATE INDEX idx_imp_sessions_started ON impersonation_sessions(started_at);

-- ============================================================================
-- ADD IMPERSONATION PERMISSION TO USERS TABLE
-- ============================================================================
ALTER TABLE users ADD COLUMN IF NOT EXISTS impersonation_permission BOOLEAN DEFAULT false;

-- ============================================================================
-- COMMENTS
-- ============================================================================
COMMENT ON TABLE impersonation_sessions IS 'Tracks admin impersonation sessions with lifecycle state, access mode, and usage metrics';
COMMENT ON COLUMN impersonation_sessions.access_mode IS 'Either read_only or full_access';
COMMENT ON COLUMN impersonation_sessions.status IS 'Session state: active, expired, or terminated';
COMMENT ON COLUMN impersonation_sessions.pages_visited IS 'JSON array of pages visited during the impersonation session';
COMMENT ON COLUMN users.impersonation_permission IS 'Whether this superadmin is permitted to use the impersonation feature';

-- ============================================================================
-- MIGRATION INFO
-- ============================================================================
-- Created: 2025-01-01
-- Version: 2.0
-- Purpose: Add impersonation sessions table and user permission flag
-- Dependencies: 001_backend_independent_schema.sql (users table must exist)
-- Rollback: DROP TABLE IF EXISTS impersonation_sessions; ALTER TABLE users DROP COLUMN IF EXISTS impersonation_permission;
