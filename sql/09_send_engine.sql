-- 09_send_engine.sql — global pacing + fair-share scheduling
--
-- Adds the controls that keep many campaigns from colliding or bursting:
--   * campaigns.priority           — 'normal' | 'high' (at most one high)
--   * collision_window_seconds     — how close two sends count as "at the
--                                    same time" and must be separated
--   * cross_gap_min/max_seconds    — the random forward push applied when two
--                                    campaigns would otherwise send together

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS priority VARCHAR(16) NOT NULL DEFAULT 'normal';

CREATE INDEX IF NOT EXISTS idx_campaigns_status_priority
    ON campaigns(status, priority);

ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS collision_window_seconds INT NOT NULL DEFAULT 5;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS cross_gap_min_seconds INT NOT NULL DEFAULT 5;
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS cross_gap_max_seconds INT NOT NULL DEFAULT 20;
