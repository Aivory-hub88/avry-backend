-- Phase 2: Backend Service Independent Database Schema
-- This creates the independent backend database on postgres-backend:5435
-- Schema: aivery_backend
-- User: backend_user

-- ============================================================================
-- USERS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(100) UNIQUE,
    password_hash VARCHAR(255),
    full_name VARCHAR(255),
    profile_picture_url VARCHAR(500),
    bio TEXT,
    phone_number VARCHAR(20),
    date_of_birth DATE,
    country VARCHAR(100),
    city VARCHAR(100),
    is_active BOOLEAN DEFAULT true,
    email_verified BOOLEAN DEFAULT false,
    email_verified_at TIMESTAMP,
    is_superadmin BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    deleted_at TIMESTAMP,
    
    CONSTRAINT check_email_format CHECK (email ~* '^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}$')
);

-- ============================================================================
-- ROLES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(50) UNIQUE NOT NULL,
    description TEXT,
    permissions TEXT[],
    is_system BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- USER ROLES JUNCTION TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS user_roles (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id UUID NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    assigned_at TIMESTAMP DEFAULT NOW(),
    assigned_by UUID REFERENCES users(id),
    
    CONSTRAINT unique_user_role UNIQUE (user_id, role_id)
);

-- ============================================================================
-- PERMISSIONS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS permissions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(100) UNIQUE NOT NULL,
    description TEXT,
    resource VARCHAR(50),
    action VARCHAR(50),
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- AUTH TOKENS TABLE (JWT tokens)
-- ============================================================================
CREATE TABLE IF NOT EXISTS auth_tokens (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token VARCHAR(500) UNIQUE NOT NULL,
    token_type VARCHAR(20) DEFAULT 'JWT',
    expires_at TIMESTAMP NOT NULL,
    is_revoked BOOLEAN DEFAULT false,
    revoked_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- SESSIONS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_token VARCHAR(500) UNIQUE NOT NULL,
    ip_address VARCHAR(45),
    user_agent TEXT,
    device_name VARCHAR(255),
    expires_at TIMESTAMP NOT NULL,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT NOW(),
    last_activity TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- PASSWORD RESETS TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS password_resets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    reset_token VARCHAR(500) UNIQUE NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    is_used BOOLEAN DEFAULT false,
    used_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- LOGIN HISTORY TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS login_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    ip_address VARCHAR(45),
    user_agent TEXT,
    login_status VARCHAR(20), -- 'success', 'failed'
    failure_reason VARCHAR(255),
    created_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- USER PREFERENCES TABLE
-- ============================================================================
CREATE TABLE IF NOT EXISTS user_preferences (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
    theme VARCHAR(20) DEFAULT 'light',
    language VARCHAR(10) DEFAULT 'en',
    notifications_enabled BOOLEAN DEFAULT true,
    email_notifications BOOLEAN DEFAULT true,
    sms_notifications BOOLEAN DEFAULT false,
    marketing_emails BOOLEAN DEFAULT true,
    two_factor_enabled BOOLEAN DEFAULT false,
    timezone VARCHAR(50) DEFAULT 'UTC',
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- INDEXES FOR PERFORMANCE
-- ============================================================================
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_users_username ON users(username);
CREATE INDEX idx_users_created_at ON users(created_at);
CREATE INDEX idx_users_is_active ON users(is_active);
CREATE INDEX idx_users_deleted_at ON users(deleted_at);

CREATE INDEX idx_auth_tokens_user_id ON auth_tokens(user_id);
CREATE INDEX idx_auth_tokens_expires_at ON auth_tokens(expires_at);
CREATE INDEX idx_auth_tokens_is_revoked ON auth_tokens(is_revoked);

CREATE INDEX idx_sessions_user_id ON sessions(user_id);
CREATE INDEX idx_sessions_expires_at ON sessions(expires_at);
CREATE INDEX idx_sessions_is_active ON sessions(is_active);

CREATE INDEX idx_user_roles_user_id ON user_roles(user_id);
CREATE INDEX idx_user_roles_role_id ON user_roles(role_id);

CREATE INDEX idx_login_history_user_id ON login_history(user_id);
CREATE INDEX idx_login_history_created_at ON login_history(created_at);

CREATE INDEX idx_password_resets_user_id ON password_resets(user_id);
CREATE INDEX idx_password_resets_expires_at ON password_resets(expires_at);

-- ============================================================================
-- DEFAULT DATA
-- ============================================================================

-- Insert default roles
INSERT INTO roles (name, description, is_system, permissions) VALUES
    ('admin', 'Administrator with full system access', true, ARRAY['*']),
    ('user', 'Regular user with basic access', true, ARRAY['read:profile', 'update:profile', 'read:diagnostics']),
    ('premium', 'Premium user with advanced features', true, ARRAY['read:profile', 'update:profile', 'read:diagnostics', 'create:diagnostics', 'access:premium_features']),
    ('superadmin', 'Super administrator - system owner', true, ARRAY['*'])
ON CONFLICT (name) DO NOTHING;

-- Insert default permissions
INSERT INTO permissions (name, description, resource, action) VALUES
    ('read:profile', 'Read user profile', 'profile', 'read'),
    ('update:profile', 'Update user profile', 'profile', 'update'),
    ('delete:profile', 'Delete user profile', 'profile', 'delete'),
    ('read:diagnostics', 'Read diagnostics', 'diagnostics', 'read'),
    ('create:diagnostics', 'Create diagnostics', 'diagnostics', 'create'),
    ('read:payments', 'Read payments', 'payments', 'read'),
    ('create:payments', 'Create payments', 'payments', 'create'),
    ('access:premium_features', 'Access premium features', 'features', 'premium'),
    ('manage:users', 'Manage users', 'users', 'manage'),
    ('manage:roles', 'Manage roles', 'roles', 'manage'),
    ('manage:system', 'Manage system settings', 'system', 'manage')
ON CONFLICT (name) DO NOTHING;

-- ============================================================================
-- COMMENTS
-- ============================================================================
COMMENT ON TABLE users IS 'Stores user account information for the backend service';
COMMENT ON TABLE roles IS 'Stores user roles and their associated permissions';
COMMENT ON TABLE auth_tokens IS 'Stores JWT tokens for user authentication';
COMMENT ON TABLE sessions IS 'Stores active user sessions';
COMMENT ON TABLE login_history IS 'Tracks login attempts and authentication events';

-- ============================================================================
-- GRANTS (for backend_user)
-- ============================================================================
GRANT USAGE ON SCHEMA public TO backend_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO backend_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO backend_user;

-- ============================================================================
-- MIGRATION INFO
-- ============================================================================
-- Created: 2026-06-05
-- Version: 1.0
-- Purpose: Phase 2 Backend Service Independent Database Schema
-- Status: Production Ready
-- Rollback: DROP SCHEMA public CASCADE; CREATE SCHEMA public;
