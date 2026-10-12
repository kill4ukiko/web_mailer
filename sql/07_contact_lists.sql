-- =============================================================================
-- 07_contact_lists.sql
-- Reusable contact lists (Thunderbird-style) with many-to-many membership.
--
--   * contact_lists         : named, reusable groups of contacts
--   * contact_list_members  : which contacts belong to a list
--   * campaign_lists        : which lists a campaign targets
--
-- A contact may belong to any number of lists; a campaign may target any
-- number of lists. Contacts can still exist with no list membership.
-- =============================================================================

CREATE TABLE IF NOT EXISTS contact_lists (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_contact_lists_name
    ON contact_lists (lower(name));

CREATE TABLE IF NOT EXISTS contact_list_members (
    list_id    UUID NOT NULL REFERENCES contact_lists(id) ON DELETE CASCADE,
    contact_id UUID NOT NULL REFERENCES contacts(id)      ON DELETE CASCADE,
    added_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (list_id, contact_id)
);

CREATE INDEX IF NOT EXISTS idx_contact_list_members_contact
    ON contact_list_members (contact_id);

CREATE TABLE IF NOT EXISTS campaign_lists (
    campaign_id UUID NOT NULL REFERENCES campaigns(id)     ON DELETE CASCADE,
    list_id     UUID NOT NULL REFERENCES contact_lists(id) ON DELETE CASCADE,
    PRIMARY KEY (campaign_id, list_id)
);

CREATE INDEX IF NOT EXISTS idx_campaign_lists_list
    ON campaign_lists (list_id);
