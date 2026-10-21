-- 15_paced_next_send.sql — persist the next allowed send slot per campaign.
--
-- Large per-email jitter must never block a worker or leave claimed rows to go
-- stale. Instead the send loop records when the next email may go out and both
-- the Celery task countdown and the cron driver honour it. NULL means "send as
-- soon as caps/window allow" (the default for existing campaigns).
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS next_send_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_campaigns_next_send ON campaigns(next_send_at);
