# SmartQueue

A responsive Flask web app for discovering nearby services, viewing queue estimates, and joining a virtual queue. It includes customer, provider, and admin demo views backed by a local JSON file store.

## Run locally

1. Install Python 3.10 or newer.
2. From this folder, create a virtual environment:
   - Windows PowerShell: `python -m venv .venv`
   - macOS/Linux: `python3 -m venv .venv`
3. Install Flask with `.venv\Scripts\python.exe -m pip install -r requirements.txt` on Windows or `.venv/bin/python -m pip install -r requirements.txt` on macOS/Linux.
4. Start the app with `.venv\Scripts\python.exe app.py` on Windows or `.venv/bin/python app.py` on macOS/Linux, then open <http://127.0.0.1:5000>.
5. Run tests with `.venv\Scripts\python.exe -m unittest discover -s tests` on Windows or `.venv/bin/python -m unittest discover -s tests` on macOS/Linux.

## Data storage

`data/seed/providers/` holds the committed sample provider records. On first startup, Flask copies them into `data/store/providers/`; subsequent provider and queue changes are persisted as individual JSON records, with a `_index.json` file in each collection. `data/store/` is git-ignored so runtime changes remain local. The storage layer exposes get, list, create, put, delete, config, and export operations.

Queue entries are tied to the signed-in customer account. Favorites, provider settings, and service status are account-scoped; customers cannot create multiple active queue entries, and a provider can only have one active service at a time. Customer queue positions and wait estimates refresh periodically while the queue page is open. This is a local-development prototype: the JSON store is intended for a single server process and local disk, and does not provide production-grade multi-instance consistency. It is not connected to a hosted database, real-time push notifications, maps, or identity verification.

## Registration

Use **Sign up** to register as a customer or service provider. Provide an email address, an international phone number (with `+` and country code), or both; any supplied contact value must be unique. Passwords are stored as salted hashes. An optional JPEG, PNG, or WebP photo (up to 5 MB) is normalized and saved locally. If other registration details need correction, the form retains the valid photo in private, session-bound staging for up to one hour, so it does not need to be uploaded again. Provider photos appear on business listing cards; customer photos remain in local storage and are not publicly served. Accounts are persisted in `data/store/users/`; the users collection index omits password hashes.

Register customers and providers with email and/or an international phone number, then sign in using that email or phone number and the account password. Customers can search listings, save and filter favorites, join one provider queue at a time, and see an updated queue position and wait estimate. Providers can edit their listing, hours, and average service duration; select a waiting customer to start serving; and mark the service complete. Provider dashboard and queue actions are restricted to the account that owns the listing, and state-changing form/API requests require a session CSRF token.

Contact details are not verified and there is no password recovery. A random session signing key is generated at app startup for local use; set a persistent, secret `SMARTQUEUE_SECRET_KEY` environment variable when running the app if sessions must survive restarts. This prototype is not a substitute for production authentication, durable database transactions, or verified accounts.

## Demo views

The customer view is the default, with fictional seed listings for preview. Sign in with a provider account to manage its listing and queue; the admin button remains an illustrative, unauthenticated demo view. Queue actions and provider settings persist in the local JSON store.
