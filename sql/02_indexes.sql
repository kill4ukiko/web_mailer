-- 02_indexes.sql — performance indexing

CREATE INDEX idx_contacts_email ON contacts(email);
CREATE INDEX idx_contacts_status ON contacts(status) WHERE status = 'active';
CREATE INDEX idx_recipients_campaign_status ON campaign_recipients(campaign_id, status);
CREATE INDEX idx_events_campaign_id ON email_events(campaign_id);