-- 22_hard_bounce_policy.sql -- per-campaign hard-bounce handling and
-- restart watermarks.
--
--   * hard_bounce_policy / hard_bounce_threshold mirror the soft-bounce
--     controls: 'suppress' (default, immediate), 'threshold' (suppress after
--     N hard bounces) or 'never' (keep the contact active; the campaign
--     circuit breaker still applies).
--   * contacts.hard_bounce_count feeds the 'threshold' policy.
--   * campaigns.stats_baseline records the cumulative stats at the moment an
--     operator restarts a paused campaign, so the circuit breaker measures
--     only the new segment and an old bounce cannot re-trip it instantly.
--     Existing campaigns keep an empty baseline (no behaviour change).

ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS hard_bounce_policy VARCHAR(16)
    NOT NULL DEFAULT 'suppress';
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS hard_bounce_threshold INT
    NOT NULL DEFAULT 2;
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS stats_baseline JSONB
    NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE contacts ADD COLUMN IF NOT EXISTS hard_bounce_count INT
    NOT NULL DEFAULT 0;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_hard_bounce_at TIMESTAMPTZ;

-- Guard the accepted policy values without blocking future migrations that
-- might extend the list (drop + add keeps the constraint idempotent).
ALTER TABLE campaigns DROP CONSTRAINT IF EXISTS campaigns_hard_bounce_policy_check;
ALTER TABLE campaigns ADD CONSTRAINT campaigns_hard_bounce_policy_check
    CHECK (hard_bounce_policy IN ('suppress', 'threshold', 'never'));
