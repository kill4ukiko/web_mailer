-- 14_reputation_lifecycle.sql — suppression durability, soft delete,
-- contact delivery lifecycle, and per-campaign soft-bounce policy.
--
-- Design notes:
--   * Suppression lives on the contact row (status + safe_to_send). A soft
--     delete preserves that state; a hard delete removes the row entirely and
--     is the explicit "clean slate" escape hatch that makes an address fully
--     reusable again.
--   * Re-importing/re-adding an existing email must NOT reset its suppression;
--     the application upsert only updates profile fields (see supabase_client).
--   * ``last_recipient_status`` / ``last_event_at`` denormalise the newest
--     delivery outcome so the contacts page can badge + filter without joining
--     campaign_recipients.
--
-- NOTE: keep every ALTER TYPE ... ADD VALUE in its own statement and never
-- *evaluate* a newly added enum value in the same migration batch (Postgres
-- refuses to use a value added in the same transaction that created it).

-- ------------------------------------------------------------------ --
-- 1. recipient_status values the webhook writes for suppression events
-- ------------------------------------------------------------------ --
ALTER TYPE recipient_status ADD VALUE IF NOT EXISTS 'unsubscribed';
ALTER TYPE recipient_status ADD VALUE IF NOT EXISTS 'complained';

-- ------------------------------------------------------------------ --
-- 2. Contact soft-delete + delivery lifecycle
-- ------------------------------------------------------------------ --
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_recipient_status VARCHAR(24);
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_event_at TIMESTAMPTZ;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS soft_bounce_count INT NOT NULL DEFAULT 0;
ALTER TABLE contacts ADD COLUMN IF NOT EXISTS last_soft_bounce_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_contacts_deleted_at ON contacts(deleted_at);
CREATE INDEX IF NOT EXISTS idx_contacts_last_status ON contacts(last_recipient_status);

-- ------------------------------------------------------------------ --
-- 3. Per-campaign soft-bounce policy
--    soft_bounce_policy: 'suppress' (immediate, default) | 'threshold' | 'never'
--    soft_bounce_threshold: bounces within the window before a 'threshold'
--                           campaign suppresses the contact.
-- ------------------------------------------------------------------ --
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS soft_bounce_policy VARCHAR(16) NOT NULL DEFAULT 'suppress';
ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS soft_bounce_threshold INT NOT NULL DEFAULT 2;
