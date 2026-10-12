-- 10_remove_qev.sql — retire QuickEmailVerification (QEV) entirely
--
-- Email verification is no longer performed by the platform. Contacts are
-- created as-is; operators are asked to verify addresses before importing.
-- Drop the QEV result column (and its derived safety flag is left in place as
-- it is still used as the send gate).

ALTER TABLE contacts
    DROP COLUMN IF EXISTS qev_result;
