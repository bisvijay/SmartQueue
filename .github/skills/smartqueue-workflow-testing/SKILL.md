---
name: smartqueue-workflow-testing
description: Run and extend SmartQueue customer/provider workflow regression checks after application changes.
---

# SmartQueue workflow testing

Use this skill when validating changes to registration, sign-in, customer discovery, favorites, queue actions, provider dashboards, or provider settings.

## Run the automated suite

From the repository root, run the complete Flask integration suite:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_app.py
```

If the virtual environment is not available, use an installed Python interpreter:

```powershell
python -m unittest discover -s tests -p test_app.py
```

Compile the changed Python modules as a quick syntax check:

```powershell
python -m py_compile app.py tests\test_app.py
```

`ApiTests` builds a Flask test client with an isolated temporary JSON store and a CSRF token. Keep workflow tests isolated in this fixture; do not use or modify `data/store/`.

## Workflow coverage checklist

Run the full suite after app changes, and add or update focused integration assertions for affected workflows:

- **Registration and login:** customer/provider creation, matching password confirmation and mismatch rejection, email or phone contact validation (including India-default `+91` normalization and explicit international codes), duplicate contacts, password hashing, photo retention after validation errors, provider listing creation, successful email/normalized-phone login, bad credentials, password reset with matching account contacts, logout, and CSRF.
- **Customer discovery:** landing page/provider listing and public provider contact links, favorites add/remove, favorites rendered in the page, paused-provider behavior, joining a queue, current position and wait estimate, moving from one waiting queue to another, and leaving a queue.
- **Provider operations:** only the owning provider can access its dashboard or mutate its listing/queues; provider settings and status changes persist; customers cannot join while paused; owner-added and public QR walk-in entries validate contact details and join the same lifecycle; contact details appear in the owner dashboard and remain private from public discovery; the QR code targets the provider walk-in form.
- **Provider listing settings:** the dashboard exposes provider-editable profile fields, including photo, business name, category, address, tagline, public contacts, services, hours, and average service time; updates persist and appear on the public listing, while ratings/reviews and live queue values remain system-managed.
- **Provider dashboard presentation:** the accessible expand/collapse control exists, expanded mode limits the display to the QR code and live queue, and the queue refresh route is owner-only.
- **Completed services:** the provider dashboard filter shows completed records with completion time and private customer contact details, keeps active queue counts and controls scoped to active entries, and restricts history to the owning provider.
- **Queue service lifecycle:** provider starts the oldest waiting customer, direct service transitions enforce valid states, only one customer is served at a time, completion updates queue metrics, and customer wait estimates refresh.
- **Photos and privacy:** provider photos are displayed through the provider media route; customer photos remain stored privately and are not served by that route.

The integration tests exercise Flask routes, rendered HTML, sessions, and persisted records together. Verify newly registered providers appear with the expected category even when their address is outside the default area; location filtering should be opt-in. When changing browser-side behavior in `static/app.js`, also inspect the corresponding template controls and exercise the changed interaction in a browser if one is available; the Flask test client does not execute JavaScript.

## Adding a regression test

Place route/workflow tests in `tests/test_app.py` under `ApiTests`. Use `self.client` for browser-like requests, `self.login_account()` to switch authenticated roles, and `self.store` to assert persisted state. Assert the response status and user-visible/API result as well as the relevant persisted state. Keep each test's created data in the fixture's temporary store.

After editing tests or app code, rerun the full suite and the Python compile check. Report any browser-only behavior that could not be exercised separately from passing server-side integration tests.
