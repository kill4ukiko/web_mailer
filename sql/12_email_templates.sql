-- Reusable email templates for the template gallery.
--
--   * email_templates : named subject + HTML body the operator can save,
--                       reuse in the campaign composer, and preview.
--
-- Names are unique case-insensitively (see the lower(name) index) so seeding
-- and manual saves are idempotent by name.
-- =============================================================================

CREATE TABLE IF NOT EXISTS email_templates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    subject             TEXT NOT NULL DEFAULT '',
    html_content        TEXT NOT NULL DEFAULT '',
    plain_text_content  TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_email_templates_name
    ON email_templates (lower(name));
