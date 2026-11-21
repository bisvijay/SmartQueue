# NextInQueue deployment runbook (Render + Supabase)

Everything needed to deploy NextInQueue to production, written down so the
steps never have to be rediscovered. The live **dev** deployment followed
exactly this path (Render service `smartqueue`, Oregon region, pointed at
the Oregon dev Supabase project) — production is the same recipe with the
Mumbai project and a Render region near it.

## How it fits together

- **Code:** GitHub repo `bisvijay/SmartQueue`, branch `main`. Render
  auto-deploys on every push to `main`.
- **App server:** gunicorn (in `requirements.txt`) running the Flask app
  factory.
- **Database:** Supabase Postgres via the **session pooler** connection
  string. If `SMARTQUEUE_DATABASE_URL` is not set, the app silently falls
  back to the local JSON file store — the startup log line tells you which
  one is active (`store=postgres` vs `store=json-file`).
- **TLS:** Supabase's pooler uses its own root CA. The app must be given
  that certificate (`supabase-root.crt`, a public root cert — not a secret)
  or every database connection fails with
  `SSLCertVerificationError: self-signed certificate in certificate chain`.

## Current deployments

| Environment | Render service | Region | Supabase project | URL |
| --- | --- | --- | --- | --- |
| Dev | `smartqueue` | Oregon (US West) | SmartQueue_dev (`tdwukdkepbtexihdaybm`, us-west-2) | https://smartqueue-1t1o.onrender.com |
| Prod | *(to create)* | Singapore (closest to Mumbai) | Production (`jubxmcxbywrticwjytzn`, ap-south-1) | — |

## Production deployment, step by step

### 1. Code readiness

- All changes merged to `main` on GitHub.
- `requirements.txt` includes `gunicorn` (it does).
- Tests pass locally: `python -m unittest discover -s tests -q`.

### 2. Prepare the production Supabase project (Mumbai)

From a PowerShell window with the **Mumbai** session-pooler URL in
`SMARTQUEUE_DATABASE_URL` and `SMARTQUEUE_DB_SSL_ROOT_CERT` pointing at the
local `supabase-root.crt`:

```powershell
python scripts\prepare_postgres.py
```

This applies every file in `migrations/` in order (001 compatibility,
002 per-customer-per-provider queue index). The migrations are safe to
re-run. Skipping this step is what causes "We couldn't add you to this
queue" when a customer joins a second queue — the database still has the
old one-active-visit-per-customer index.

For a brand-new empty project, run the baseline schema from `README.md`
first, then the command above; add `--import-json` only for a first-time
import into empty tables.

### 3. Collect the values (enter them only inside Render — never in git or chat)

- **Database URL:** Supabase → production project → **Connect** → *Session
  pooler* connection string. Replace `[YOUR-DATABASE-PASSWORD]` with the
  real database password. Shape:
  `postgresql://postgres.jubxmcxbywrticwjytzn:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres`
- **Root certificate:** the `supabase-root.crt` file (kept locally at
  `C:\Users\bisvi\supabase-root.crt`; also downloadable from the Supabase
  dashboard under Project Settings → Database → SSL configuration).
- **Admin password:** choose one for the NextInQueue admin login.
- **Secret key:** let Render's **Generate** button create it.

### 4. Create the Render web service

Render dashboard → **New + → Web Service** → repo `bisvijay/SmartQueue`:

- **Name:** `smartqueue-prod` (or your choice)
- **Region:** Singapore (keep the service near the Mumbai database;
  the dev service uses Oregon because its database is in us-west-2)
- **Branch:** `main`
- **Runtime:** Python 3
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn --bind 0.0.0.0:$PORT --threads 4 'app:create_app()'`
- **Instance Type:** Free sleeps after ~15 idle minutes (~45 s wake-up);
  pick a paid instance for production if the wake-up delay is unacceptable.

### 5. Secret file FIRST, then environment variables

> **Gotcha learned the hard way:** an unsaved Secret File draft is
> discarded if you switch to editing environment variables. Add the
> secret file and click **the Secret Files section's own
> "Save, rebuild, and deploy"** before touching anything else.

1. **Environment → Secret Files → Add Secret File**
   - Filename: `supabase-root.crt`
   - Contents: paste the full certificate (`-----BEGIN CERTIFICATE-----` …)
   - Click **Done**, then the Secret Files **Save, rebuild, and deploy**.
2. **Environment → Environment Variables** — add:
   - `SMARTQUEUE_DATABASE_URL` = the Mumbai pooler string from step 3
   - `SMARTQUEUE_ADMIN_PASSWORD` = the admin password from step 3
   - `SMARTQUEUE_SECRET_KEY` = use **Generate**
   - `SMARTQUEUE_DB_SSL_ROOT_CERT` = `/etc/secrets/supabase-root.crt`
3. Save — Render redeploys automatically.

### 6. Verify (checklist)

- [ ] Service URL loads the NextInQueue home page with provider listings
      (not a JSON `{"error": ...}` page).
- [ ] Render **Logs** show `SmartQueue app created: store=postgres`.
- [ ] Logs show `GET /` returning **200**, and no
      `SSLCertVerificationError` or `FileNotFoundError`.
- [ ] Register + log in as a customer; join **two** providers' queues;
      leave one.
- [ ] As a provider: serve and complete a customer; check the Service
      column shows the booked services.
- [ ] From a phone, scan a provider's QR code and join via the public
      walk-in page (works now that the app has a public URL).
- [ ] Admin login works with the password from step 3.

## Day-2 operations

- **Ship a change:** merge to `main` → Render auto-deploys. Watch the
  deploy in the Render dashboard; check Logs afterwards.
- **Roll back:** Render service → **Deploys** → pick the previous deploy →
  **Rollback**.
- **Point the app at a different database:** change
  `SMARTQUEUE_DATABASE_URL` in Environment (the cert file/variable stay
  the same for any Supabase project) → save → redeploy. Run
  `prepare_postgres.py` against the new database first if it has never
  had the migrations.
- **App logs:** the app logs events (`queue.join`, `auth.login`,
  `provider.approval`, API errors, Postgres reconnect warnings) to the
  Render Logs tab. Level can be raised with `SMARTQUEUE_LOG_LEVEL=DEBUG`
  (adds per-request timings).

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Every page 500s; logs show `SSLCertVerificationError … self-signed certificate in certificate chain` | App doesn't trust Supabase's root CA | Secret file + `SMARTQUEUE_DB_SSL_ROOT_CERT` (step 5) |
| Logs show `FileNotFoundError` for the cert path | Env var is set but the secret file is missing (unsaved draft) | Re-add the secret file and click **its** Save button (step 5 gotcha) |
| Logs show `store=json-file` | `SMARTQUEUE_DATABASE_URL` not set on the service | Add it in Environment and redeploy |
| "We couldn't add you to this queue" on a customer's second queue | Migration 002 not applied to that database | Run `prepare_postgres.py` against it (step 2) |
| Admin page says admin access is not configured (503) | `SMARTQUEUE_ADMIN_PASSWORD` not set | Add it in Environment and redeploy |
| First load after idle takes ~45 s | Free-plan spin-down | Expected; use a paid instance to avoid it |
