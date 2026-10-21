-- =============================================================================
-- 18_bird_sms.sql - isolated Bird SMS channel (Bird platform, bk_ keys)
--
-- Separate contacts/lists/templates/settings so Bird data never mixes with the
-- Brevo SMS data. Bird accepts one recipient per request and caps free-text
-- messages at 12 segments; delivery is asynchronous via signed webhooks.
-- =============================================================================

DO $$ BEGIN
    CREATE TYPE bird_sms_contact_status AS ENUM (
        'active', 'unsubscribed', 'bounced_hard', 'bounced_soft', 'invalid', 'blacklisted'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE bird_sms_campaign_status AS ENUM (
        'draft', 'scheduled', 'sending', 'held', 'paused', 'completed', 'failed', 'cancelled'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE bird_sms_recipient_status AS ENUM (
        'queued', 'sending', 'sent', 'accepted', 'delivered', 'undelivered',
        'expired', 'soft_bounce', 'hard_bounce', 'failed', 'skipped',
        'blacklisted', 'rejected', 'unsubscribed'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS bird_sms_contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    phone VARCHAR(24) UNIQUE NOT NULL,
    country VARCHAR(2),
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    status bird_sms_contact_status NOT NULL DEFAULT 'active',
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
CREATE INDEX IF NOT EXISTS idx_bird_sms_contacts_deleted_at ON bird_sms_contacts(deleted_at);
CREATE INDEX IF NOT EXISTS idx_bird_sms_contacts_last_status ON bird_sms_contacts(last_recipient_status);

CREATE TABLE IF NOT EXISTS bird_sms_lists (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bird_sms_lists_name ON bird_sms_lists (lower(name));

CREATE TABLE IF NOT EXISTS bird_sms_list_members (
    list_id UUID NOT NULL REFERENCES bird_sms_lists(id) ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES bird_sms_contacts(id) ON DELETE CASCADE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (list_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_bird_sms_list_members_contact ON bird_sms_list_members (contact_id);

CREATE TABLE IF NOT EXISTS bird_sms_templates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    category VARCHAR(16) NOT NULL DEFAULT 'transactional',
    include_stop_code BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_bird_sms_templates_name ON bird_sms_templates (lower(name));

CREATE TABLE IF NOT EXISTS bird_sms_campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    from_number VARCHAR(32) NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '',
    message_mode VARCHAR(16) NOT NULL DEFAULT 'text',
    category VARCHAR(16) NOT NULL DEFAULT 'transactional',
    template_ref VARCHAR(128),
    template_slug VARCHAR(255),
    template_scope VARCHAR(16),
    template_language VARCHAR(16),
    template_name VARCHAR(255),
    template_category VARCHAR(16),
    template_parameters JSONB DEFAULT '{}'::jsonb,
    include_stop_code BOOLEAN NOT NULL DEFAULT false,
    smart_encoding BOOLEAN NOT NULL DEFAULT true,
    tags JSONB DEFAULT '[]'::jsonb,
    batch_size INT NOT NULL DEFAULT 250,
    send_rate_per_minute INT,
    stagger_seconds INT,
    priority VARCHAR(16) NOT NULL DEFAULT 'normal',
    status bird_sms_campaign_status NOT NULL DEFAULT 'draft',
    circuit_state circuit_state NOT NULL DEFAULT 'ok',
    stats JSONB DEFAULT '{"sent": 0, "accepted": 0, "delivered": 0, "undelivered": 0, "failed": 0, "skipped": 0, "segments": 0, "cost": 0}'::jsonb,
    schedule_at TIMESTAMPTZ,
    next_send_at TIMESTAMPTZ,
    window_start TIME,
    window_end TIME,
    window_timezone VARCHAR(64),
    held_reason VARCHAR(32),
    held_until TIMESTAMPTZ,
    reuse_root_id UUID,
    soft_bounce_policy VARCHAR(16) NOT NULL DEFAULT 'suppress',
    soft_bounce_threshold INT NOT NULL DEFAULT 2,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_bird_sms_campaigns_status_priority ON bird_sms_campaigns(status, priority);
CREATE INDEX IF NOT EXISTS idx_bird_sms_campaigns_next_send ON bird_sms_campaigns(next_send_at);

CREATE TABLE IF NOT EXISTS bird_sms_recipients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES bird_sms_campaigns(id) ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES bird_sms_contacts(id) ON DELETE CASCADE,
    status bird_sms_recipient_status NOT NULL DEFAULT 'queued',
    segments INT,
    cost NUMERIC(12,6),
    message_id TEXT,
    sent_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    claimed_at TIMESTAMPTZ,
    last_error TEXT,
    error_code TEXT,
    error_description TEXT,
    carrier_error_code TEXT,
    CONSTRAINT unique_bird_sms_campaign_contact UNIQUE (campaign_id, contact_id)
);
CREATE INDEX IF NOT EXISTS idx_bird_sms_recipients_campaign_status ON bird_sms_recipients (campaign_id, status);
CREATE INDEX IF NOT EXISTS idx_bird_sms_recipients_message_id ON bird_sms_recipients (message_id);
CREATE INDEX IF NOT EXISTS idx_bird_sms_recipients_sent_at ON bird_sms_recipients (sent_at);

CREATE TABLE IF NOT EXISTS bird_sms_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID REFERENCES bird_sms_campaigns(id) ON DELETE CASCADE,
    recipient_id UUID REFERENCES bird_sms_recipients(id) ON DELETE CASCADE,
    event_type VARCHAR(64) NOT NULL,
    webhook_id TEXT UNIQUE,
    message_id TEXT,
    cost NUMERIC(12,6),
    raw_payload JSONB DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_bird_sms_events_campaign ON bird_sms_events (campaign_id);

CREATE TABLE IF NOT EXISTS bird_sms_campaign_lists (
    campaign_id UUID NOT NULL REFERENCES bird_sms_campaigns(id) ON DELETE CASCADE,
    list_id UUID NOT NULL REFERENCES bird_sms_lists(id) ON DELETE CASCADE,
    PRIMARY KEY (campaign_id, list_id)
);
CREATE INDEX IF NOT EXISTS idx_bird_sms_campaign_lists_list ON bird_sms_campaign_lists (list_id);

CREATE TABLE IF NOT EXISTS bird_sms_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    from_number VARCHAR(32) NOT NULL DEFAULT '',
    sender_type VARCHAR(16) NOT NULL DEFAULT 'alphanumeric',
    default_category VARCHAR(16) NOT NULL DEFAULT 'transactional',
    smart_encoding BOOLEAN NOT NULL DEFAULT true,
    include_stop_code BOOLEAN NOT NULL DEFAULT false,
    default_country_code VARCHAR(8),
    cost_per_segment NUMERIC(6,3) NOT NULL DEFAULT 0.01,
    low_budget_threshold NUMERIC(10,2) NOT NULL DEFAULT 10.0,
    max_hard_bounce_rate NUMERIC(4,3) NOT NULL DEFAULT 0.030,
    sending_paused BOOLEAN NOT NULL DEFAULT false,
    default_batch_size INT NOT NULL DEFAULT 250,
    send_rate_per_minute INT NOT NULL DEFAULT 30,
    max_recipients_per_hour INT NOT NULL DEFAULT 200,
    max_recipients_per_day INT NOT NULL DEFAULT 1000,
    max_cost_per_day NUMERIC(10,2) NOT NULL DEFAULT 20.0,
    default_stagger_seconds INT NOT NULL DEFAULT 0,
    default_window_start TIME,
    default_window_end TIME,
    default_window_timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
    warmup_enabled BOOLEAN NOT NULL DEFAULT false,
    warmup_start_date DATE,
    public_base_url VARCHAR(512),
    collision_window_seconds INT NOT NULL DEFAULT 5,
    cross_gap_min_seconds INT NOT NULL DEFAULT 5,
    cross_gap_max_seconds INT NOT NULL DEFAULT 20,
    bird_api_key TEXT NOT NULL DEFAULT '',
    bird_management_api_key TEXT NOT NULL DEFAULT '',
    bird_region VARCHAR(16) NOT NULL DEFAULT 'us1',
    bird_workspace_id VARCHAR(128),
    bird_webhook_secret TEXT NOT NULL DEFAULT '',
    telegram_bot_token TEXT NOT NULL DEFAULT '',
    telegram_chat_id TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE OR REPLACE FUNCTION claim_bird_sms_recipients(p_campaign_id UUID, p_limit INT)
RETURNS TABLE(recipient_id UUID, contact_id UUID, phone VARCHAR, first_name VARCHAR,
              last_name VARCHAR, country VARCHAR, metadata JSONB)
LANGUAGE plpgsql AS $$
BEGIN
    RETURN QUERY
    WITH claimed AS (
        SELECT r.id
        FROM bird_sms_recipients r
        JOIN bird_sms_contacts c ON c.id = r.contact_id
        WHERE r.campaign_id = p_campaign_id
          AND r.status = 'queued'
          AND c.status = 'active'
          AND c.safe_to_send = true
          AND c.deleted_at IS NULL
        ORDER BY r.id
        FOR UPDATE OF r SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE bird_sms_recipients r
       SET status = 'sending', claimed_at = now()
      FROM bird_sms_contacts c
     WHERE r.id IN (SELECT id FROM claimed)
       AND c.id = r.contact_id
    RETURNING r.id, r.contact_id, c.phone, c.first_name, c.last_name, c.country, c.metadata;
END $$;

CREATE OR REPLACE FUNCTION requeue_stale_bird_sms_recipients(p_timeout_seconds INT DEFAULT 900)
RETURNS INT
LANGUAGE plpgsql AS $$
DECLARE affected INT;
BEGIN
    UPDATE bird_sms_recipients
       SET status = 'queued', claimed_at = NULL
     WHERE status = 'sending'
       AND claimed_at < now() - make_interval(secs => p_timeout_seconds);
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END $$;

CREATE OR REPLACE FUNCTION suppress_bird_sms_contact(p_phone VARCHAR, p_reason VARCHAR)
RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE bird_sms_contacts
       SET status = p_reason::bird_sms_contact_status,
           safe_to_send = false,
           updated_at = now()
     WHERE phone = p_phone;
END $$;
