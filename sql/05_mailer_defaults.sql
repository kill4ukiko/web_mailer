-- 05_mailer_defaults.sql — global campaign defaults
--
-- Adds the per-application default sending settings to mailer_settings. New
-- campaigns inherit these unless the operator overrides them per-campaign on
-- the campaign page. Sender identity and bounce/complaint thresholds already
-- live on this table; aliases are retained.

ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS default_batch_size INT NOT NULL DEFAULT 500;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS default_batch_interval_ms INT NOT NULL DEFAULT 1000;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS default_throttle_rps INT NOT NULL DEFAULT 50;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS default_stagger_seconds INT NOT NULL DEFAULT 0;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS default_window_start TIME;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS default_window_end TIME;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS default_window_timezone VARCHAR(64) NOT NULL DEFAULT 'UTC';
