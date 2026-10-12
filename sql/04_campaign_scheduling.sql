-- 04_campaign_scheduling.sql — scheduling windows, stagger, recovery support
--
-- Adds per-campaign scheduling/staggering columns, a claimed_at marker used
-- by the stuck-campaign reconciler, a 'skipped' recipient status, and the
-- requeue_stale_recipients() recovery function.

-- ------------------------------------------------------------------ --
-- 1. Campaign scheduling / delivery-window settings
-- ------------------------------------------------------------------ --
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS schedule_at TIMESTAMPTZ;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS window_start TIME;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS window_end TIME;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS window_timezone VARCHAR(64) NOT NULL DEFAULT 'UTC';
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS stagger_seconds INT NOT NULL DEFAULT 0;

-- ------------------------------------------------------------------ --
-- 2. Recipient claiming marker (for stale-claim recovery)
-- ------------------------------------------------------------------ --
ALTER TABLE campaign_recipients ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

-- ------------------------------------------------------------------ --
-- 3. New terminal status for recipients skipped during completion
--    (ineligible / suppressed contacts never sent to).
--    NOTE: keep this in its own statement; never *evaluate* the new value
--    in the same migration batch.
-- ------------------------------------------------------------------ --
ALTER TYPE recipient_status ADD VALUE IF NOT EXISTS 'skipped';

-- ------------------------------------------------------------------ --
-- 4. claim_recipients(): record claimed_at + return contact metadata
-- ------------------------------------------------------------------ --
DROP FUNCTION IF EXISTS claim_recipients(UUID, INT);

CREATE OR REPLACE FUNCTION claim_recipients(
    p_campaign_id UUID,
    p_limit INT
)
RETURNS TABLE (
    recipient_id UUID,
    contact_id UUID,
    email VARCHAR(255),
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    metadata JSONB
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH target_recipients AS (
        SELECT cr.id, c.id AS contact_id, c.email, c.first_name, c.last_name, c.metadata
        FROM campaign_recipients cr
        JOIN contacts c ON cr.contact_id = c.id
        WHERE cr.campaign_id = p_campaign_id
          AND cr.status = 'queued'
          AND c.status = 'active'
          AND c.safe_to_send = true
        ORDER BY cr.id
        FOR UPDATE OF cr SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE campaign_recipients cr
    SET status = 'sending',
        claimed_at = NOW()
    FROM target_recipients tr
    WHERE cr.id = tr.id
    RETURNING cr.id AS recipient_id, tr.contact_id, tr.email, tr.first_name,
              tr.last_name, tr.metadata;
END;
$$;

-- ------------------------------------------------------------------ --
-- 5. requeue_stale_recipients(): recover claims abandoned by a crashed
--    worker. Returns the number of recipients put back on the queue.
-- ------------------------------------------------------------------ --
CREATE OR REPLACE FUNCTION requeue_stale_recipients(p_timeout_seconds INT)
RETURNS INT
LANGUAGE plpgsql
AS $$
DECLARE
    affected INT;
BEGIN
    UPDATE campaign_recipients
    SET status = 'queued',
        claimed_at = NULL
    WHERE status = 'sending'
      AND (claimed_at IS NULL OR claimed_at < NOW() - make_interval(secs => p_timeout_seconds));
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected;
END;
$$;

-- ------------------------------------------------------------------ --
-- 6. Indexes for the scheduler / reconciler lookups
-- ------------------------------------------------------------------ --
CREATE INDEX IF NOT EXISTS idx_campaigns_status_schedule
    ON campaigns(status, schedule_at);
CREATE INDEX IF NOT EXISTS idx_recipients_status_claimed
    ON campaign_recipients(status, claimed_at);
