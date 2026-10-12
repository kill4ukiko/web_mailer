-- 03_functions.sql — stored functions (atomic claiming, suppression)

-- Atomic Recipient Reservation using Row-Level Locking
CREATE OR REPLACE FUNCTION claim_recipients(
    p_campaign_id UUID,
    p_limit INT
)
RETURNS TABLE (
    recipient_id UUID,
    contact_id UUID,
    email VARCHAR(255),
    first_name VARCHAR(100),
    last_name VARCHAR(100)
)
LANGUAGE plpgsql
AS $$
BEGIN
    RETURN QUERY
    WITH target_recipients AS (
        SELECT cr.id, c.id AS contact_id, c.email, c.first_name, c.last_name
        FROM campaign_recipients cr
        JOIN contacts c ON cr.contact_id = c.id
        WHERE cr.campaign_id = p_campaign_id
          AND cr.status = 'queued'
          AND c.status = 'active'
          AND c.safe_to_send = true
        ORDER BY cr.id
        FOR UPDATE OF cr SKIP LOCKED
        LIMIT p_limit
    )
    UPDATE campaign_recipients cr
    SET status = 'sending'
    FROM target_recipients tr
    WHERE cr.id = tr.id
    RETURNING cr.id AS recipient_id, tr.contact_id, tr.email, tr.first_name, tr.last_name;
END;
$$;

-- Global Blacklisting & Suppression Handler
CREATE OR REPLACE FUNCTION suppress_contact(
    p_email VARCHAR(255),
    p_reason contact_status
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE contacts
    SET status = p_reason,
        safe_to_send = false,
        updated_at = NOW()
    WHERE email = LOWER(TRIM(p_email));
END;
$$;