-- Atomic campaign counter updates.
--
-- campaigns.stats is JSONB and was previously incremented with a
-- read-modify-write in Python. Concurrent tracking pixels and webhooks could
-- then overwrite each other (lost updates). This function performs the
-- increment inside a single UPDATE so the database row lock serialises it.
CREATE OR REPLACE FUNCTION increment_campaign_stat(
    p_campaign_id UUID,
    p_field TEXT,
    p_delta NUMERIC DEFAULT 1
)
RETURNS NUMERIC
LANGUAGE plpgsql
AS $$
DECLARE
    new_value NUMERIC;
BEGIN
    -- Guard against arbitrary keys; mirrors SupabaseWrapper.STATS_FIELDS.
    IF p_field NOT IN (
        'sent', 'delivered', 'opens', 'clicks', 'bounces', 'complaints',
        'unsubscribes', 'failed', 'hard_bounce', 'soft_bounce'
    ) THEN
        RAISE EXCEPTION 'unknown campaign stats field: %', p_field;
    END IF;

    UPDATE campaigns
       SET stats = jsonb_set(
               COALESCE(stats, '{}'::jsonb),
               ARRAY[p_field],
               to_jsonb(COALESCE((stats ->> p_field)::numeric, 0) + p_delta),
               true
           )
     WHERE id = p_campaign_id
    RETURNING (stats ->> p_field)::numeric INTO new_value;

    RETURN new_value;
END;
$$;
