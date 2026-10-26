-- 21_site_accounts.sql - public GoPriLad website accounts and consent audit.

CREATE TABLE IF NOT EXISTS site_users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'active', 'disabled')),
    email_verified_at TIMESTAMPTZ,
    verification_token_hash TEXT,
    verification_expires_at TIMESTAMPTZ,
    verification_sent_at TIMESTAMPTZ,
    marketing_consent BOOLEAN NOT NULL DEFAULT FALSE,
    consent_version VARCHAR(32),
    consent_at TIMESTAMPTZ,
    consent_withdrawn_at TIMESTAMPTZ,
    consent_ip VARCHAR(45),
    consent_user_agent TEXT,
    terms_version VARCHAR(32),
    terms_accepted_at TIMESTAMPTZ,
    privacy_version VARCHAR(32),
    privacy_accepted_at TIMESTAMPTZ,
    contact_id UUID REFERENCES contacts(id) ON DELETE SET NULL,
    last_login_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_site_users_verification_token
    ON site_users (verification_token_hash)
    WHERE verification_token_hash IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_site_users_status
    ON site_users (status, created_at);

CREATE INDEX IF NOT EXISTS idx_site_users_contact
    ON site_users (contact_id);
