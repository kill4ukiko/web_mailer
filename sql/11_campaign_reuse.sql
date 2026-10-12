-- 11_campaign_reuse.sql — campaign reuse (clone / resend) lineage
--
-- A reused campaign (cloned into a new draft, or the same campaign resent)
-- shares a "reuse_root_id" so de-duplication can be applied per lineage: a
-- contact already emailed anywhere in the thread is excluded from "new only"
-- sends. For the very first campaign the column stays NULL and the campaign's
-- own id acts as the root.

ALTER TABLE campaigns
    ADD COLUMN IF NOT EXISTS reuse_root_id UUID;

-- Backfill any existing rows so the root is explicit on current data.
UPDATE campaigns
   SET reuse_root_id = id
 WHERE reuse_root_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_campaigns_reuse_root_id
    ON campaigns (reuse_root_id);
