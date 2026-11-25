-- Customer reviews of completed queue visits. The review itself (rating)
-- is a column; everything else (visit, customer, comment) lives in app_data,
-- following the provider_reviews pattern.
CREATE TABLE IF NOT EXISTS customer_reviews (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id uuid NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    rating integer NOT NULL CHECK (rating BETWEEN 1 AND 5),
    created_at timestamptz NOT NULL DEFAULT now(),
    app_data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS customer_reviews_provider_time_idx
    ON customer_reviews (provider_id, created_at DESC);
