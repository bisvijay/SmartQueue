-- Additive compatibility changes for the SmartQueue Flask app.
-- Run after the baseline schema in README.md.

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS app_data jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE accounts
    DROP CONSTRAINT IF EXISTS accounts_role_check;
ALTER TABLE accounts
    ADD CONSTRAINT accounts_role_check
    CHECK (role IN ('customer', 'provider', 'admin', 'supervisor'));

ALTER TABLE providers
    ALTER COLUMN owner_account_id DROP NOT NULL;
ALTER TABLE providers
    ADD COLUMN IF NOT EXISTS approval_status text NOT NULL DEFAULT 'approved'
        CHECK (approval_status IN ('pending', 'approved', 'rejected'));
ALTER TABLE providers
    ADD COLUMN IF NOT EXISTS approval_note varchar(500) NOT NULL DEFAULT '';
ALTER TABLE providers
    ADD COLUMN IF NOT EXISTS approval_updated_at timestamptz;
ALTER TABLE providers
    ADD COLUMN IF NOT EXISTS app_data jsonb NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE queue_visits
    ADD COLUMN IF NOT EXISTS app_data jsonb NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS provider_reviews (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    decision text NOT NULL CHECK (decision IN ('approved', 'rejected', 'resubmitted')),
    note varchar(500) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    app_data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS provider_reviews_provider_time_idx
    ON provider_reviews (provider_id, created_at DESC);
