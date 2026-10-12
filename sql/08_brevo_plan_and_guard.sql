-- 08_brevo_plan_and_guard.sql — record the operator's Brevo plan
--
-- The plan drives the settings guard: it supplies the recommended prefill
-- values and the hard per-plan limits that bound the global pacing knobs.

ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS brevo_plan VARCHAR(32) NOT NULL DEFAULT 'free';
