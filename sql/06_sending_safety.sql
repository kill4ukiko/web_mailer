-- 06_sending_safety.sql — pacing, caps and safety controls
--
-- Replaces the confusing batch_interval_ms / throttle_rps pair with a single
-- human "send rate" (emails per minute), and adds the guardrails that keep a
-- misconfiguration from hammering the SMTP provider:
--   * send_rate_per_minute       — global aggregate ceiling (safe default 120/min)
--   * max_recipients_per_hour    — hard hourly cap across all campaigns
--   * max_recipients_per_day     — hard daily cap across all campaigns
--   * sending_paused             — global kill switch honoured by every worker
--   * reply_to_email             — optional Reply-To header
--   * operator_email             — where test sends / alerts go
--   * public_base_url            — public URL used for tracking + unsubscribe
--   * allowed_sender_domains     — extra domains allowed in a campaign From
--   * warmup_*                   — optional ramp-up window for a new domain

ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS send_rate_per_minute INT NOT NULL DEFAULT 120;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS max_recipients_per_hour INT NOT NULL DEFAULT 2000;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS max_recipients_per_day INT NOT NULL DEFAULT 8000;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS sending_paused BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS reply_to_email VARCHAR(255);
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS operator_email VARCHAR(255);
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS public_base_url VARCHAR(512);
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS allowed_sender_domains JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS warmup_enabled BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS warmup_start_date DATE;

-- Per-campaign override of the global sending rate (never above the global cap).
ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS send_rate_per_minute INT;

-- Speed up the hourly/daily cap counters (sent_at windows).
CREATE INDEX IF NOT EXISTS idx_recipients_sent_at
    ON campaign_recipients(sent_at) WHERE sent_at IS NOT NULL;
