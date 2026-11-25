-- =====================================================================
-- YouAreNext — COMPLETE DATABASE SCHEMA (single file)
-- =====================================================================
-- This file creates EVERY table, index and constraint the app needs,
-- in the right order, on an EMPTY Postgres database (e.g. a brand-new
-- Supabase project). It is the concatenation of:
--   PART 1  the baseline schema (same SQL as README.md's schema section)
--   PART 2+ migrations/001-006, verbatim, in order
--
-- How to use on a new environment:
--   Option A (SQL only): paste this whole file into the Supabase
--     SQL Editor and run it once. Done — no other SQL is needed.
--   Option B (script): run the baseline + migrations separately via
--     scripts/prepare_postgres.py (it applies migrations/*.sql in
--     order; run the baseline first). Use this for an EXISTING
--     database that only needs newer migrations — the script is
--     safe to re-run, this full file is meant for empty databases.
--
-- MAINTENANCE RULE: whenever a new migration is added under
-- migrations/, append its SQL here (as a new PART) in the same
-- change, and update the migration table in docs/DEPLOYMENT.md.
-- =====================================================================


-- =====================================================================
-- PART 1 — Baseline schema
-- =====================================================================

CREATE TABLE media_assets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_account_id uuid NOT NULL,
    provider_id uuid,
    purpose text NOT NULL CHECK (purpose IN ('profile_photo', 'business_photo')),
    visibility text NOT NULL CHECK (visibility IN ('private', 'public')),
    object_key text NOT NULL UNIQUE,
    content_type text NOT NULL
        CHECK (content_type IN ('image/jpeg', 'image/png', 'image/webp')),
    byte_size integer NOT NULL CHECK (byte_size BETWEEN 1 AND 5242880),
    width integer,
    height integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    CHECK (
        (purpose = 'profile_photo' AND provider_id IS NULL AND visibility = 'private')
        OR (purpose = 'business_photo' AND provider_id IS NOT NULL)
    )
);

CREATE TABLE accounts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    role text NOT NULL CHECK (role IN ('customer', 'provider')),
    name varchar(100) NOT NULL,
    email varchar(254),
    phone_e164 varchar(16),
    password_hash varchar(255) NOT NULL,
    profile_media_id uuid REFERENCES media_assets(id) ON DELETE SET NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deactivated_at timestamptz,
    CHECK (email IS NOT NULL OR phone_e164 IS NOT NULL),
    CHECK (phone_e164 IS NULL OR phone_e164 ~ '^\+[1-9][0-9]{7,14}$')
);

CREATE UNIQUE INDEX accounts_email_unique
    ON accounts (lower(email))
    WHERE email IS NOT NULL AND deactivated_at IS NULL;
CREATE UNIQUE INDEX accounts_phone_unique
    ON accounts (phone_e164)
    WHERE phone_e164 IS NOT NULL AND deactivated_at IS NULL;

CREATE TABLE providers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_account_id uuid NOT NULL UNIQUE REFERENCES accounts(id) ON DELETE RESTRICT,
    name varchar(120) NOT NULL,
    category varchar(40) NOT NULL,
    tagline varchar(160) NOT NULL DEFAULT '',
    address varchar(200) NOT NULL,
    city varchar(100),
    state_region varchar(100),
    postal_code varchar(20),
    country_code char(2) NOT NULL DEFAULT 'IN',
    timezone text NOT NULL DEFAULT 'Asia/Kolkata',
    public_email varchar(254),
    public_phone_e164 varchar(16),
    business_media_id uuid REFERENCES media_assets(id) ON DELETE SET NULL,
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'paused', 'archived')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (
        public_phone_e164 IS NULL
        OR public_phone_e164 ~ '^\+[1-9][0-9]{7,14}$'
    )
);

CREATE INDEX providers_discovery_idx
    ON providers (country_code, state_region, city, category, status);

CREATE TABLE provider_services (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    name varchar(60) NOT NULL,
    estimated_minutes smallint NOT NULL
        CHECK (estimated_minutes BETWEEN 1 AND 180),
    is_active boolean NOT NULL DEFAULT true,
    display_order smallint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider_id, name)
);
CREATE INDEX provider_services_active_idx
    ON provider_services (provider_id, display_order) WHERE is_active;

CREATE TABLE provider_hours (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    weekday smallint NOT NULL CHECK (weekday BETWEEN 1 AND 7),
    interval_order smallint NOT NULL DEFAULT 0,
    opens_at time NOT NULL,
    closes_at time NOT NULL,
    CHECK (closes_at > opens_at),
    UNIQUE (provider_id, weekday, interval_order)
);

CREATE TABLE provider_hour_exceptions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    local_date date NOT NULL,
    is_closed boolean NOT NULL DEFAULT false,
    opens_at time,
    closes_at time,
    note varchar(120),
    CHECK (
        (is_closed AND opens_at IS NULL AND closes_at IS NULL)
        OR (NOT is_closed AND opens_at IS NOT NULL
            AND closes_at IS NOT NULL AND closes_at > opens_at)
    ),
    UNIQUE (provider_id, local_date)
);

CREATE TABLE provider_favorites (
    customer_account_id uuid NOT NULL REFERENCES accounts(id) ON DELETE RESTRICT,
    provider_id uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_account_id, provider_id)
);

CREATE TABLE queue_visits (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    customer_account_id uuid REFERENCES accounts(id) ON DELETE SET NULL,
    service_id uuid REFERENCES provider_services(id) ON DELETE SET NULL,
    customer_name varchar(100) NOT NULL,
    customer_email varchar(254),
    customer_phone_e164 varchar(16),
    service_name varchar(60) NOT NULL,
    source text NOT NULL
        CHECK (source IN ('customer_app', 'public_qr', 'provider_dashboard')),
    status text NOT NULL
        CHECK (status IN ('waiting', 'serving', 'completed',
                          'cancelled', 'left', 'no_show')),
    joined_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    ended_at timestamptz,
    CHECK (
        customer_phone_e164 IS NULL
        OR customer_phone_e164 ~ '^\+[1-9][0-9]{7,14}$'
    )
);
CREATE INDEX queue_visits_provider_history_idx
    ON queue_visits (provider_id, joined_at DESC);
CREATE INDEX queue_visits_provider_status_idx
    ON queue_visits (provider_id, status, joined_at);
CREATE INDEX queue_visits_customer_history_idx
    ON queue_visits (customer_account_id, joined_at DESC)
    WHERE customer_account_id IS NOT NULL;
CREATE UNIQUE INDEX queue_one_active_visit_per_customer
    ON queue_visits (customer_account_id)
    WHERE customer_account_id IS NOT NULL
      AND status IN ('waiting', 'serving');
CREATE UNIQUE INDEX queue_one_serving_visit_per_provider
    ON queue_visits (provider_id) WHERE status = 'serving';

CREATE TABLE queue_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    visit_id uuid NOT NULL REFERENCES queue_visits(id) ON DELETE RESTRICT,
    event_type text NOT NULL
        CHECK (event_type IN ('joined', 'started', 'completed',
                              'cancelled', 'left', 'switched', 'no_show')),
    actor_account_id uuid REFERENCES accounts(id) ON DELETE SET NULL,
    occurred_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX queue_events_visit_time_idx
    ON queue_events (visit_id, occurred_at);

CREATE TABLE daily_queue_metrics (
    provider_id uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    service_name varchar(60) NOT NULL DEFAULT '',
    local_date date NOT NULL,
    timezone text NOT NULL,
    arrivals_count integer NOT NULL DEFAULT 0 CHECK (arrivals_count >= 0),
    completed_count integer NOT NULL DEFAULT 0 CHECK (completed_count >= 0),
    cancelled_count integer NOT NULL DEFAULT 0 CHECK (cancelled_count >= 0),
    left_count integer NOT NULL DEFAULT 0 CHECK (left_count >= 0),
    no_show_count integer NOT NULL DEFAULT 0 CHECK (no_show_count >= 0),
    average_wait_seconds integer,
    p50_wait_seconds integer,
    p90_wait_seconds integer,
    average_service_seconds integer,
    calculated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider_id, service_name, local_date)
);

ALTER TABLE media_assets
    ADD CONSTRAINT media_assets_owner_fk
    FOREIGN KEY (owner_account_id) REFERENCES accounts(id) ON DELETE RESTRICT;
ALTER TABLE media_assets
    ADD CONSTRAINT media_assets_provider_fk
    FOREIGN KEY (provider_id) REFERENCES providers(id) ON DELETE RESTRICT;

-- =====================================================================
-- PART 2 — migrations/001_postgres_app_compat.sql
-- App compatibility: app_data columns, provider approval fields, provider_reviews table
-- =====================================================================

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

-- =====================================================================
-- PART 3 — migrations/002_multi_queue_per_customer.sql
-- One active visit per customer per provider (was: per customer)
-- =====================================================================

-- Allow a customer to hold active visits at several providers at once.
-- Replaces the one-active-visit-per-customer rule with one active visit
-- per customer per provider. Run after 001_postgres_app_compat.sql
-- (scripts/prepare_postgres.py applies every file in this folder in order).

DROP INDEX IF EXISTS queue_one_active_visit_per_customer;

CREATE UNIQUE INDEX IF NOT EXISTS queue_one_active_visit_per_customer_provider
    ON queue_visits (customer_account_id, provider_id)
    WHERE customer_account_id IS NOT NULL
      AND status IN ('waiting', 'serving');

-- =====================================================================
-- PART 4 — migrations/003_multi_staff_serving.sql
-- Drop the one-serving-per-provider index (limit moves to the app's staffCount)
-- =====================================================================

-- Allow several customers of one provider to be served at the same time
-- (multi-staff serving). The per-provider limit now lives in the app
-- (the provider's staffCount setting, default 1) and is enforced while
-- the provider row is locked; a database index can only express a fixed
-- limit of one, so the one-serving-per-provider index goes away.
-- Run after 002_multi_queue_per_customer.sql (scripts/prepare_postgres.py
-- applies every file in this folder in order).

DROP INDEX IF EXISTS queue_one_serving_visit_per_provider;

-- =====================================================================
-- PART 5 — migrations/004_provider_photos.sql
-- provider_photos table (durable photo storage in the database)
-- =====================================================================

-- Provider photos move from local disk files to database rows so uploads
-- survive hosts with ephemeral filesystems (e.g. Render free services,
-- where every deploy or restart wipes local files). The image bytes are
-- stored base64-encoded in the data column (JPEG, at most 1600px).
CREATE TABLE IF NOT EXISTS provider_photos (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    provider_id uuid NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    data text NOT NULL,
    content_type text NOT NULL DEFAULT 'image/jpeg',
    created_at timestamptz NOT NULL DEFAULT now(),
    app_data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS provider_photos_provider_idx
    ON provider_photos (provider_id);

-- =====================================================================
-- PART 6 — migrations/005_advertisements.sql
-- advertisements, ad_media and ad_events tables
-- =====================================================================

-- Advertisement module: ads, their media, and basic delivery events.
-- Ad configuration lives in app_data (like provider_reviews); media bytes
-- are stored base64-encoded in ad_media.data because the production host's
-- filesystem is ephemeral (the same reason provider photos live in the
-- database). Uploads are size-capped by the application (images 5 MB,
-- videos 10 MB) so no large files are stored here.
CREATE TABLE IF NOT EXISTS advertisements (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT now(),
    app_data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE TABLE IF NOT EXISTS ad_media (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ad_id uuid NOT NULL REFERENCES advertisements(id) ON DELETE CASCADE,
    data text NOT NULL,
    content_type text NOT NULL DEFAULT 'application/octet-stream',
    created_at timestamptz NOT NULL DEFAULT now(),
    app_data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ad_media_ad_idx
    ON ad_media (ad_id);
CREATE TABLE IF NOT EXISTS ad_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ad_id uuid NOT NULL REFERENCES advertisements(id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    app_data jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ad_events_ad_idx
    ON ad_events (ad_id, created_at);

-- =====================================================================
-- PART 7 — migrations/006_customer_reviews.sql
-- customer_reviews table
-- =====================================================================

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
