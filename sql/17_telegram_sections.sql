-- Per-section Telegram alert configuration. Empty strings mean "fall back to
-- TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the environment".
ALTER TABLE mailer_settings
    ADD COLUMN IF NOT EXISTS telegram_bot_token TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT NOT NULL DEFAULT '';

ALTER TABLE sms_settings
    ADD COLUMN IF NOT EXISTS telegram_bot_token TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS telegram_chat_id TEXT NOT NULL DEFAULT '';
