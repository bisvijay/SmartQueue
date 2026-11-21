-- Allow several customers of one provider to be served at the same time
-- (multi-staff serving). The per-provider limit now lives in the app
-- (the provider's staffCount setting, default 1) and is enforced while
-- the provider row is locked; a database index can only express a fixed
-- limit of one, so the one-serving-per-provider index goes away.
-- Run after 002_multi_queue_per_customer.sql (scripts/prepare_postgres.py
-- applies every file in this folder in order).

DROP INDEX IF EXISTS queue_one_serving_visit_per_provider;
