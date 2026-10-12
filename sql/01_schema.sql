-- 01_schema.sql — base tables & enum definitions

CREATE TYPE contact_status AS ENUM (
    'active', 'unsubscribed', 'bounced_hard', 'bounced_soft', 'invalid', 'complained'
);

CREATE TYPE campaign_status AS ENUM (
    'draft', 'scheduled', 'sending', 'paused', 'completed', 'failed', 'cancelled'
);

CREATE TYPE recipient_status AS ENUM (
    'queued', 'sending', 'sent', 'delivered', 'deferred', 'soft_bounce', 'hard_bounce', 'failed'
);

CREATE TYPE circuit_state AS ENUM (
    'ok', 'warning', 'paused', 'blocked'
);

-- 1. CONTACTS
CREATE TABLE contacts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email VARCHAR(255) UNIQUE NOT NULL,
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    status contact_status NOT NULL DEFAULT 'active',
    engagement_score NUMERIC(3, 2) DEFAULT 1.00,
    timezone VARCHAR(50) DEFAULT 'UTC',
    qev_result VARCHAR(50),
    safe_to_send BOOLEAN DEFAULT true,
    metadata JSONB DEFAULT '{}'::jsonb,
    imported_at TIMESTAMPTZ DEFAULT NOW(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. CAMPAIGNS
CREATE TABLE campaigns (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    subject VARCHAR(255) NOT NULL,
    from_email VARCHAR(255) NOT NULL,
    from_name VARCHAR(255) NOT NULL,
    html_content TEXT NOT NULL,
    plain_text_content TEXT,
    batch_size INT NOT NULL DEFAULT 500,
    batch_interval_ms INT NOT NULL DEFAULT 1000,
    throttle_rps INT NOT NULL DEFAULT 50,
    circuit_state circuit_state NOT NULL DEFAULT 'ok',
    status campaign_status NOT NULL DEFAULT 'draft',
    stats JSONB DEFAULT '{"sent": 0, "delivered": 0, "opens": 0, "clicks": 0, "bounces": 0, "complaints": 0}'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

-- 3. CAMPAIGN RECIPIENTS (Junction Table)
CREATE TABLE campaign_recipients (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID NOT NULL REFERENCES campaigns(id) ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
    status recipient_status NOT NULL DEFAULT 'queued',
    sent_at TIMESTAMPTZ,
    opened_at TIMESTAMPTZ,
    clicked_at TIMESTAMPTZ,
    last_error TEXT,
    CONSTRAINT unique_campaign_contact UNIQUE (campaign_id, contact_id)
);

-- 4. EMAIL EVENTS (Tracking & Inbound Webhooks)
CREATE TABLE email_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id UUID REFERENCES campaigns(id) ON DELETE CASCADE,
    recipient_id UUID REFERENCES campaign_recipients(id) ON DELETE CASCADE,
    event_type VARCHAR(50) NOT NULL,
    url_clicked TEXT,
    ip_address VARCHAR(45),
    user_agent TEXT,
    raw_payload JSONB DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ DEFAULT NOW()
);

-- 5. SYSTEM SETTINGS
CREATE TABLE mailer_settings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    sender_email VARCHAR(255) NOT NULL,
    sender_name VARCHAR(255) NOT NULL,
    sender_domain VARCHAR(255) NOT NULL,
    max_hard_bounce_rate NUMERIC(4,3) DEFAULT 0.020,
    max_complaint_rate NUMERIC(4,3) DEFAULT 0.001,
    aliases JSONB DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);