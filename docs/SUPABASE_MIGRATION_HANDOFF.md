# Supabase migration handoff

## Current status

- The application uses the local JSON `FileStore` by default.
- Setting `SMARTQUEUE_DATABASE_URL` opts the Flask app into `PostgresStore`, implemented in `postgres_store.py`.
- The Postgres adapter stores accounts, providers, provider services, favorites, queue visits, and provider approval reviews in relational tables. Additional compatibility fields are held in `app_data` JSONB columns.
- `migrations/001_postgres_app_compat.sql` adapts the baseline schema in `README.md` for the Flask app. It adds app-data columns, provider approval fields, the approval review table, admin/supervisor role values, and nullable owners for fictional seed listings.
- `scripts/prepare_postgres.py` applies that migration. With `--import-json`, it imports JSON accounts, providers, favorites, queue visits, and approval reviews and remaps legacy IDs to stable UUIDs.
- The importer refuses to run when its principal target tables are non-empty. It does not delete or modify source JSON.
- Provider/customer image bytes still live in local files; the Supabase Storage migration is not implemented.
- Flask continues to handle login/password hashing. Supabase Auth is not configured. Provider approval admin access still uses `SMARTQUEUE_ADMIN_PASSWORD`; database-backed admin creation and role/permission management are not implemented.

## Supabase setup and first import

Use the same PowerShell window for configuration, migration, import, and app startup. Never paste the URI, database password, or certificate into chat or commit them.

1. Copy the PostgreSQL URI from the Supabase project **Connect** dialog. Use the session pooler if the network is IPv4-only or direct connectivity fails. URL-encode reserved characters in the database password.
2. Set the private connection URI in PowerShell:

   ```powershell
   $env:SMARTQUEUE_DATABASE_URL = "your-postgresql-connection-uri"
   ```

3. If TLS certificate verification fails, download the Supabase database root certificate from **Database Settings → SSL Configuration → Download Certificate**, save it outside the repository, and set:

   ```powershell
   $env:SMARTQUEUE_DB_SSL_ROOT_CERT = "$env:USERPROFILE\supabase-root.crt"
   ```

   Do not disable TLS or certificate verification.

4. From the repository root, install requirements and apply the compatibility migration plus data import:

   ```powershell
   python -m pip install -r requirements.txt
   python scripts\prepare_postgres.py --import-json
   ```

   `scripts\prepare_postgres.py` without `--import-json` applies only the compatibility SQL migration. The importer requires an empty target and stops rather than attempting an implicit merge.

5. If the import succeeds, launch the app in the same shell:

   ```powershell
   python app.py
   ```

6. Verify account login, provider discovery, queue joins, provider queue actions, provider approvals, and Supabase row counts. Keep a separate backup of the JSON store until verification and restore testing are complete.

## Known limitations and cautions

- No live Supabase connection or import has been verified from the development environment. The previous connection attempt failed certificate validation; the root-certificate option is implemented but must be tested with the user's private connection details.
- Existing project schema must match the baseline DDL in `README.md` plus the compatibility migration. The compatibility migration is intended for that schema, not arbitrary pre-existing schemas.
- Import does not migrate image bytes to Supabase Storage. Existing `photoId` metadata remains in app data, so photo files must remain available locally for photo rendering.
- The compatibility migration allows `admin` and `supervisor` values in `accounts.role`, but the public registration flow does not create those roles and the app does not yet enforce a database-backed permissions model.
- The current admin approval dashboard is still protected by the single configured `SMARTQUEUE_ADMIN_PASSWORD`.
- Production use still needs reviewed authorization policies, account provisioning, operational backups, and a tested recovery process. Do not expose the database connection URI in browser code.

## Validation performed

- Flask workflow integration suite: `python -m unittest discover -s tests -p test_app.py` — 41 tests passed.
- Python compilation: `python -m py_compile app.py postgres_store.py scripts\prepare_postgres.py tests\test_app.py`.
- JavaScript syntax: `node --check static\app.js`.
- Whitespace/diff validation: `git diff --check`.
- These checks do not replace a real Supabase migration/import and post-migration workflow test.

