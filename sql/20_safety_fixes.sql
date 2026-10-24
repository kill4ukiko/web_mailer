-- 20_safety_fixes.sql — campaign error + reputation audit bookkeeping.
--
-- last_error is written by the completion guard (and reputation pauses) so
-- operators can see why a campaign was stopped without digging through logs.
-- A breach claims reputation_checked_at immediately; a healthy campaign is
-- finalized after a 24-hour settle window so late bounces are still caught.

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS reputation_checked_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_campaigns_reputation_pending
    ON campaigns(finished_at)
    WHERE status = 'completed' AND reputation_checked_at IS NULL;
