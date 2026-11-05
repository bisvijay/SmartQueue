# SmartQueue

SmartQueue is a responsive queue-management app for customers and service providers. Customers discover businesses, check live queue estimates, and join a queue; providers manage their listing, handle walk-ins, serve customers, and review completed services. The current app is a Flask prototype backed by a local JSON file store.

## App details

- **Customer experience:** browse and filter providers, view public business contacts and services, save favorites, join one queue at a time, and see an updated position and estimated wait.
- **Provider experience:** manage the business listing and photo, open or pause the queue, add in-person customers, serve the next customer, review completed services, and display a QR code that opens the provider’s walk-in page.
- **Queue lifecycle:** visits move through waiting and serving to completed, or can end as left, cancelled, or no-show. Queue activity and customer contact details are available to the customer and the owning provider, not to public discovery.
- **Phone numbers:** an explicit `+` country code is preserved. A number without one defaults to India and is stored in E.164 form with `+91`.
- **Scope:** SmartQueue is for managing queues and business listings only. Do not collect diagnoses, symptoms, insurance details, or other health information.

## Run locally

1. Install Python 3.10 or newer.
2. From this folder, create a virtual environment:
   - Windows PowerShell: `python -m venv .venv`
   - macOS/Linux: `python3 -m venv .venv`
3. Install Flask with `.venv\Scripts\python.exe -m pip install -r requirements.txt` on Windows or `.venv/bin/python -m pip install -r requirements.txt` on macOS/Linux.
4. Start the app with `.venv\Scripts\python.exe app.py` on Windows or `.venv/bin/python app.py` on macOS/Linux, then open <http://127.0.0.1:5000>.
5. Run tests with `.venv\Scripts\python.exe -m unittest discover -s tests` on Windows or `.venv/bin/python -m unittest discover -s tests` on macOS/Linux.

## Current data storage

`data/seed/providers/` holds the committed sample provider records. On first startup, Flask copies them into `data/store/providers/`; subsequent provider and queue changes are persisted as individual JSON records, with a `_index.json` file in each collection. `data/store/` is git-ignored so runtime changes remain local. The storage layer exposes get, list, create, put, delete, config, and export operations.

Queue entries are tied to the signed-in customer account. Favorites, provider settings, and service status are account-scoped; customers cannot create multiple active queue entries, and a provider can only have one active service at a time. Customer queue positions and wait estimates refresh periodically while the queue page is open. This is a local-development prototype: the JSON store is intended for a single server process and local disk, and does not provide production-grade multi-instance consistency. It is not connected to a hosted database, real-time push notifications, maps, or identity verification.

## Recommended production data design

The following is a **proposed future design**, not a description of the current implementation. The app still uses the JSON store above; no database or cloud storage migration has been performed. For India-first deployment, a managed PostgreSQL project in Mumbai (`ap-south-1`) is a good starting point. Supabase provides Postgres and object storage in one service and currently lists Mumbai as a region. Its Free plan can support a pilot, but projects may pause after inactivity and have storage, backup, and availability limits. Keep independent database exports and review current [regional availability](https://supabase.com/docs/guides/platform/regions) and [pricing](https://supabase.com/pricing) before launch. Run the Flask app in or near the selected database region too.

Use PostgreSQL as the source of truth for accounts, provider listings, services, hours, favorites, queue visits, and queue event history. Store timestamps as `timestamptz` in UTC; use the provider timezone (default `Asia/Kolkata`) when applying business hours and grouping local-day reports. Store telephone numbers in E.164 format: preserve an explicitly supplied country code and apply `+91` only when the user omitted one.

### Tables and relationships

| Table | Contents and relationships |
|---|---|
| `accounts` | Customer/provider account, role, name, unique normalized email and/or phone, password hash, profile-image reference, creation and deactivation times. |
| `providers` | Business listing owned by one provider account; name, category, tagline, address, country/region/city/postal code, timezone, public contact, business-image reference, status. |
| `provider_services` | Services offered by a provider, estimated duration, active flag, and display order. |
| `provider_hours` | One row per weekly opening interval, supporting multiple intervals per weekday. |
| `provider_hour_exceptions` | Date-specific closures or replacement opening hours for holidays and special dates. |
| `provider_favorites` | Many-to-many customer/provider favorites. |
| `queue_visits` | One durable record per customer or walk-in visit, with provider/service, source, status, contact snapshot, and lifecycle timestamps. |
| `queue_events` | Append-only audit history for join, start, completion, cancellation, leave, switch, and no-show actions. |
| `media_assets` | Metadata and opaque object-storage key for profile or business images; no image bytes in the database. |
| `daily_queue_metrics` | Aggregated counts and wait/service-time statistics by provider, service, local date, and timezone; no customer identity or contact fields. |

Important relationships: one account may own one provider listing; a provider has many services, hours, exceptions, visits, and business images over time; an account may have many visits and favorites; a visit may have many events. Keep service-name and customer-name snapshots on each visit so past records remain readable after an account or listing changes. A walk-in has no account foreign key but still has the contact details the provider needs to manage that visit. Store weekly hours in local wall-clock time with the provider timezone; represent an overnight opening interval as intervals split across adjacent weekdays.

### PostgreSQL schema

This is a baseline DDL sketch for migrations. Production migrations should also establish database roles, permissions, indexes appropriate to observed query load, and a retention/deletion policy.

```sql
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
```

In an actual migration, create `accounts`, `providers`, and `media_assets` first without their circular foreign keys, then add the foreign keys after all referenced tables exist.

### Write and read flows

1. **Register:** validate and normalize email/phone, hash the password, create an account, and—if the role is provider—create its business listing and initial services in one transaction.
2. **Join:** verify the provider is open and the customer has no active visit, then insert a `queue_visits` row and `joined` event in one transaction. The database partial unique indexes protect against concurrent requests violating queue rules.
3. **Serve/complete:** lock the provider’s active queue rows in a transaction, enforce one current service, update the visit status/timestamp, and append its event.
4. **Leave/cancel/switch:** update the existing visit to its terminal state and append an event instead of deleting it. This preserves trustworthy operational history without implying that an ended visit is active.
5. **History and analytics:** give the customer their visit history and give providers only visits belonging to their provider account. Build forecasts from aggregate arrival, wait, service-duration, and outcome metrics—not names, phone numbers, emails, or profile images.
6. **Public discovery:** return provider listing fields and public business contacts only. Never return queue customer details from discovery endpoints.

### Image storage

Store image bytes in an object-storage service such as **Supabase Storage**; Postgres stores only a `media_assets` row with an opaque object key, owner, purpose, visibility, content type, size, dimensions, and timestamps.

- Use separate buckets or equivalent policies for public business images and private account profile images. Business photos shown on public listings may be public; customer profile photos should remain private and require authorization or short-lived signed URLs.
- Generate random keys such as `providers/<provider-uuid>/<random-uuid>.webp`; do not use original filenames or put email/phone/name in paths.
- Validate the actual image format and byte/pixel limits, normalize and strip metadata (including EXIF location), and serve only supported image content types.
- On replacement, upload and verify the new object, update the database reference, then remove the prior object. Periodically clean up unreferenced uploads.
- Do not upload queue records or health information into image storage. Monitor free-tier quotas and keep a separate backup/export plan.

### Low-cost rollout and migration

For a low-volume pilot, begin on Supabase Free in Mumbai, with alerts/usage checks, scheduled database exports to a separate location, and a tested restore procedure. Upgrade or move to a paid managed plan before depending on it for continuous operation or backups; the free tier can pause after inactivity. Keep Flask and Postgres in nearby regions to reduce round-trip latency.

Migration should be incremental: add SQLAlchemy models and Alembic migrations; import users, providers, favorites, and queue records from the JSON store; verify row counts and login/queue workflows; switch the store adapter to Postgres; then retain a final JSON export as a rollback archive. Add object storage for images in a separate step, first migrating provider photos and verifying public rendering, then customer photos with private access. The current local JSON store remains the active implementation until that migration is explicitly built and configured.

## Registration

Use **Sign up** to register as a customer or service provider. Provide an email address, a phone number, or both; any supplied contact value must be unique. Phone numbers with an explicit `+` country code keep that code; numbers without one are treated as Indian numbers and normalized to `+91` (10 digits, or 11 digits with a leading domestic `0`). Enter the password twice to catch typos; passwords are stored as salted hashes. The login page links to a local-demo password reset that requires both registered email and phone to match, then asks for the new password twice. The app does not verify contact ownership or send reset codes, so this reset flow is only suitable for a trusted local prototype and must be replaced with a verified recovery process before deployment. An optional JPEG, PNG, or WebP photo (up to 5 MB) is normalized and saved locally. If other registration details need correction, the form retains the valid photo in private, session-bound staging for up to one hour, so it does not need to be uploaded again. Provider photos appear on business listing cards; customer photos remain in local storage and are not publicly served. Accounts are persisted in `data/store/users/`; the users collection index omits password hashes.

Register customers and providers with email and/or a phone number, then sign in using that email or phone number and the account password. Numbers without a `+` country code default to India (`+91`); explicit international country codes are preserved. A provider’s registered email and phone are initially used as its public business contact; the provider can edit them independently in business settings. Providers can update their listing photo, name, category, address, 160-character tagline, business contacts, services, opening hours, and average service duration from the owner-only dashboard. Queue counts, ratings, and reviews are managed by SmartQueue. Customers can search listings, filter by category or an optional location, save favorites, join one provider queue at a time, and see an updated queue position and wait estimate. Scanning a provider dashboard QR code opens a public walk-in form for that provider. Providers can add walk-in customers with contact details; select a waiting customer to start serving; and mark the service complete. The provider dashboard filters between active queue entries and completed service history; completed entries remain visible only to the owning provider. Walk-in customer contact details are available only in the owning provider’s dashboard/API, not public provider discovery. The dashboard can expand to a QR-and-live-queue display and refreshes queue activity automatically. State-changing form/API requests require a session CSRF token.

Contact details are not verified. A random session signing key is generated at app startup for local use; set a persistent, secret `SMARTQUEUE_SECRET_KEY` environment variable when running the app if sessions must survive restarts. This prototype is not a substitute for production authentication, durable database transactions, or verified accounts.

## Demo views

The customer view is the default, with fictional seed listings for preview. Sign in with a provider account to manage its listing and queue; the admin button remains an illustrative, unauthenticated demo view. Queue actions and provider settings persist in the local JSON store.

## Example scenarios

### Customer joins a queue

1. A customer registers with an email, a phone, or both. A phone entered without a country code is treated as Indian (`+91`); an explicit international code is kept.
2. The customer searches or filters providers, checks the service and wait estimate, and selects **Join queue**.
3. SmartQueue records the visit and shows the customer their current position and estimated wait. They may leave while waiting; that visit should remain in history as `left`.

### Provider serves a walk-in

1. A provider registers a business and edits its listing, contact details, services, hours, and average service time from the dashboard.
2. A visitor scans the provider’s QR code or the provider adds the visitor from the dashboard; either way, the visit joins the same queue lifecycle.
3. The provider starts the next waiting visit, completes service, and can later filter the dashboard to review completed services. The visitor’s contact is visible to the owning provider, not public provider search.

### Forecast demand without profiling customers

1. Completed and ended visits contribute timestamps and outcomes to operational metrics.
2. A scheduled aggregation groups arrivals, wait durations, service durations, and cancellation/leave/no-show rates by provider, service, weekday, and local hour.
3. SmartQueue can use those aggregates to improve wait estimates and show providers expected busy periods. The forecast does not require customer names, phone numbers, email addresses, or health details.
