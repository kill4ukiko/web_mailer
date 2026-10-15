-- 13_campaign_hold.sql — daily/hourly cap "held" state support
--
-- When the shared global send cap is exhausted, a sending campaign is moved to
-- a distinct 'held' status instead of silently idling in 'sending'. The resume
-- task flips it back to 'sending' automatically once the cap window resets.
--
-- NOTE: keep the ALTER TYPE in its own statement and never *evaluate* the new
-- enum value in this migration batch (Postgres refuses to use a value added in
-- the same transaction that created it).

-- ------------------------------------------------------------------ --
-- 1. New campaign status for cap-held campaigns
-- ------------------------------------------------------------------ --
ALTER TYPE campaign_status ADD VALUE IF NOT EXISTS 'held';

-- ------------------------------------------------------------------ --
-- 2. Hold bookkeeping columns
--    held_reason: 'day' | 'hour' (which cap is exhausted)
--    held_until : when the cap is expected to reset (UTC boundary)
-- ------------------------------------------------------------------ --
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS held_reason VARCHAR(16);
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS held_until TIMESTAMPTZ;
