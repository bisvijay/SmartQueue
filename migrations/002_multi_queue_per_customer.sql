-- Allow a customer to hold active visits at several providers at once.
-- Replaces the one-active-visit-per-customer rule with one active visit
-- per customer per provider. Run after 001_postgres_app_compat.sql
-- (scripts/prepare_postgres.py applies every file in this folder in order).

DROP INDEX IF EXISTS queue_one_active_visit_per_customer;

CREATE UNIQUE INDEX IF NOT EXISTS queue_one_active_visit_per_customer_provider
    ON queue_visits (customer_account_id, provider_id)
    WHERE customer_account_id IS NOT NULL
      AND status IN ('waiting', 'serving');
