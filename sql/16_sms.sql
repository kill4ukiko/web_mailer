-- =============================================================================
-- 16_sms.sql — SMS channel (Brevo transactional SMS)
--
-- A fully parallel, isolated SMS stack mirroring the email tables. Nothing here
-- touches the email tables/RPCs, so the existing emailer keeps working exactly
-- as before. The application only sends transactional SMS via
-- POST /v3/transactionalSMS/send, paced by our own queue.
--
-- Design notes:
--   * SMS contacts are phone-keyed (E.164), independent of email contacts.
--   * Suppression (status + safe_to_send) lives on the contact and is preserved
--     by re-import (the app upserts profile fields only).
--   * Sender IDs are limited to 11 alphanumeric / 15 numeric chars by Brevo.
--   * Credits are the scarce resource: we track per-recipient segments/credits
--     and enforce a daily credit ceiling in addition to hourly/day caps.
-- =============================================================================

-- ------------------------------------------------------------------ --
-- 1. Enums
-- ------------------------------------------------------------------ --
DO $$ BEGIN
    CREATE TYPE sms_contact_status AS ENUM (
        'active', 'unsubscribed', 'bounced_hard', 'bounced_soft', 'invalid', 'blacklisted'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE sms_campaign_status AS ENUM (
        'draft', 'scheduled', 'sending', 'held', 'paused', 'completed', 'failed', 'cancelled'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE sms_recipient_status AS ENUM (
        'queued', 'sending', 'sent', 'accepted', 'delivered', 'replied',
        'soft_bounce', 'hard_bounce', 'failed', 'skipped', 'blacklisted',
        'rejected', 'unsubscribed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ------------------------------------------------------------------ --
-- 2. SMS contacts (phone-keyed address book)
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS sms_contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone VARCHAR(24) UNIQUE NOT NULL,
    country VARCHAR(2),
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    status sms_contact_status NOT NULL DEFAULT 'active',
    safe_to_send BOOLEAN NOT NULL DEFAULT true,
    deleted_at TIMESTAMPTZ,
    last_recipient_status VARCHAR(24),
    last_event_at TIMESTAMPTZ,
    soft_bounce_count INT NOT NULL DEFAULT 0,
    last_soft_bounce_at TIMESTAMPTZ,
    metadata JSONB DEFAULT '{}'::jsonb,
    imported_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sms_contacts_deleted_at ON sms_contacts(deleted_at);
CREATE INDEX IF NOT EXISTS idx_sms_contacts_last_status ON sms_contacts(last_recipient_status);

-- ------------------------------------------------------------------ --
-- 3. SMS lists
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS sms_lists (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sms_lists_name ON sms_lists (lower(name));

CREATE TABLE IF NOT EXISTS sms_list_members (
    list_id    UUID NOT NULL REFERENCES sms_lists(id)    ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES sms_contacts(id) ON DELETE CASCADE,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (list_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_sms_list_members_contact
    ON sms_list_members (contact_id);

-- ------------------------------------------------------------------ --
-- 4. SMS campaigns
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS sms_campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    sender VARCHAR(15) NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    sms_type VARCHAR(16) NOT NULL DEFAULT 'transactional',
    unicode_enabled BOOLEAN NOT NULL DEFAULT false,
    organisation_prefix VARCHAR(64),
    include_stop_code BOOLEAN NOT NULL DEFAULT false,
    tags JSONB DEFAULT '[]'::jsonb,
    batch_size INT NOT NULL DEFAULT 500,
    send_rate_per_minute INT,
    stagger_seconds INT,
    priority VARCHAR(16) NOT NULL DEFAULT 'normal',
    status sms_campaign_status NOT NULL DEFAULT 'draft',
    circuit_state circuit_state NOT NULL DEFAULT 'ok',
    stats JSONB DEFAULT '{"sent": 0, "accepted": 0, "delivered": 0, "replied": 0, "bounces": 0, "skipped": 0, "failed": 0, "segments": 0, "credits_used": 0}'::jsonb,
    schedule_at TIMESTAMPTZ,
    next_send_at TIMESTAMPTZ,
    window_start TIME,
    window_end TIME,
    window_timezone VARCHAR(64),
    held_reason VARCHAR(16),
    held_until TIMESTAMPTZ,
    reuse_root_id UUID,
    soft_bounce_policy VARCHAR(16) NOT NULL DEFAULT 'suppress',
    soft_bounce_threshold INT NOT NULL DEFAULT 2,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sms_campaigns_status_priority
    ON sms_campaigns(status, priority);
CREATE INDEX IF NOT EXISTS idx_sms_campaigns_next_send
    ON sms_campaigns(next_send_at);

-- ------------------------------------------------------------------ --
-- 5. SMS recipients (junction with delivery bookkeeping)
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS sms_recipients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES sms_campaigns(id)  ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES sms_contacts(id)    ON DELETE CASCADE,
    status sms_recipient_status NOT NULL DEFAULT 'queued',
    segments INT,
    credits_used NUMERIC(10,3),
    message_id TEXT,
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    last_error TEXT,
    CONSTRAINT unique_sms_campaign_contact UNIQUE (campaign_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_sms_recipients_campaign_status
    ON sms_recipients (campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_sms_recipients_sent_at
    ON sms_recipients (sent_at);

-- ------------------------------------------------------------------ --
-- 6. SMS events (delivery webhooks)
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS sms_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID REFERENCES sms_campaigns(id)  ON DELETE CASCADE,
    recipient_id UUID REFERENCES sms_recipients(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    raw_payload JSONB DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------------ --
-- 7. Campaign -> list targeting
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS sms_campaign_lists (
    campaign_id UUID NOT NULL REFERENCES sms_campaigns(id) ON DELETE CASCADE,
    list_id     UUID NOT NULL REFERENCES sms_lists(id)     ON DELETE CASCADE,
    PRIMARY KEY (campaign_id, list_id)
);

CREATE INDEX IF NOT EXISTS idx_sms_campaign_lists_list
    ON sms_campaign_lists (list_id);

-- ------------------------------------------------------------------ --
-- 8. SMS templates
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS sms_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    sms_type VARCHAR(16) NOT NULL DEFAULT 'transactional',
    unicode_enabled BOOLEAN NOT NULL DEFAULT false,
    organisation_prefix VARCHAR(64),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sms_templates_name
    ON sms_templates (lower(name));

-- ------------------------------------------------------------------ --
-- 9. SMS settings (single row, mirrors mailer_settings)
-- ------------------------------------------------------------------ --
CREATE TABLE IF NOT EXISTS sms_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sms_sender VARCHAR(15) NOT NULL DEFAULT '',
    sms_sender_type VARCHAR(16) NOT NULL DEFAULT 'alphanumeric',
    default_sms_type VARCHAR(16) NOT NULL DEFAULT 'transactional',
    unicode_enabled BOOLEAN NOT NULL DEFAULT false,
    organisation_prefix VARCHAR(64),
    include_stop_code BOOLEAN NOT NULL DEFAULT false,
    default_country_code VARCHAR(8),
    credit_cost_per_segment NUMERIC(6,3) NOT NULL DEFAULT 1.0,
    low_credit_threshold INT NOT NULL DEFAULT 50,
    max_hard_bounce_rate NUMERIC(4,3) NOT NULL DEFAULT 0.030,
    max_complaint_rate NUMERIC(4,3) NOT NULL DEFAULT 0.005,
    sending_paused BOOLEAN NOT NULL DEFAULT false,
    default_batch_size INT NOT NULL DEFAULT 500,
    send_rate_per_minute INT NOT NULL DEFAULT 30,
    max_recipients_per_hour INT NOT NULL DEFAULT 200,
    max_recipients_per_day INT NOT NULL DEFAULT 1000,
    max_credits_per_day INT NOT NULL DEFAULT 1000,
    default_stagger_seconds INT NOT NULL DEFAULT 0,
    default_window_start TIME,
    default_window_end TIME,
    default_window_timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
    warmup_enabled BOOLEAN NOT NULL DEFAULT false,
    warmup_start_date DATE,
    sms_plan VARCHAR(32) NOT NULL DEFAULT 'trial',
    public_base_url VARCHAR(512),
    collision_window_seconds INT NOT NULL DEFAULT 5,
    cross_gap_min_seconds INT NOT NULL DEFAULT 5,
    cross_gap_max_seconds INT NOT NULL DEFAULT 20,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- ------------------------------------------------------------------ --
-- 10. Stored procedures (atomic operations, mirror the email RPCs)
-- ------------------------------------------------------------------ --
CREATE OR REPLACE FUNCTION claim_sms_recipients(p_campaign_id UUID, p_limit INT)
RETURNS TABLE(recipient_id UUID, contact_id UUID, phone VARCHAR, first_name VARCHAR,
              last_name VARCHAR, country VARCHAR, metadata JSONB)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    WITH claimed AS (
        SELECT r.id
        FROM sms_recipients r
        JOIN sms_contacts c ON c.id = r.contact_id
        WHERE r.campaign_id = p_campaign_id
          AND r.status = 'queued'
          AND c.status = 'active'
          AND c.safe_to_send = true
          AND c.deleted_at IS NULL
        ORDER BY r.id
        FOR UPDATE OF r SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE sms_recipients r
       SET status = 'sending', claimed_at = now()
      FROM sms_contacts c
     WHERE r.id IN (SELECT id FROM claimed)
       AND c.id = r.contact_id
    RETURNING r.id, r.contact_id, c.phone, c.first_name, c.last_name, c.country, c.metadata;
END $$;

CREATE OR REPLACE FUNCTION requeue_stale_sms_recipients(p_timeout_seconds INT DEFAULT 900)
RETURNS INT
LANGUAGE plpgsql AS $$
DECLARE
    affected INT;
BEGIN
    UPDATE sms_recipients
       SET status = 'queued', claimed_at = NULL
     WHERE status = 'sending'
       AND claimed_at < now() - make_interval(secs => p_timeout_seconds);
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END $$;

CREATE OR REPLACE FUNCTION suppress_sms_contact(p_phone VARCHAR, p_reason VARCHAR)
RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE sms_contacts
       SET status = p_reason::sms_contact_status,
           safe_to_send = false,
           updated_at = now()
     WHERE phone = p_phone;
END $$;
