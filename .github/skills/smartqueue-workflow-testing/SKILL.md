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

- **Registration and login:** customer/provider creation, email or phone contact validation, duplicate contacts, password hashing, photo retention after validation errors, provider listing creation, successful login, bad credentials, logout, and CSRF.
- **Customer discovery:** landing page/provider listing, favorites add/remove, favorites rendered in the page, paused-provider behavior, joining a queue, current position and wait estimate, moving from one waiting queue to another, and leaving a queue.
- **Provider operations:** only the owning provider can access its dashboard or mutate its listing/queues; provider settings and status changes persist; customers cannot join while paused.
- **Queue service lifecycle:** provider starts the oldest waiting customer, direct service transitions enforce valid states, only one customer is served at a time, completion updates queue metrics, and customer wait estimates refresh.
- **Photos and privacy:** provider photos are displayed through the provider media route; customer photos remain stored privately and are not served by that route.

The integration tests exercise Flask routes, rendered HTML, sessions, and persisted records together. When changing browser-side behavior in `static/app.js`, also inspect the corresponding template controls and exercise the changed interaction in a browser if one is available; the Flask test client does not execute JavaScript.

## Adding a regression test

Place route/workflow tests in `tests/test_app.py` under `ApiTests`. Use `self.client` for browser-like requests, `self.login_account()` to switch authenticated roles, and `self.store` to assert persisted state. Assert the response status and user-visible/API result as well as the relevant persisted state. Keep each test's created data in the fixture's temporary store.

After editing tests or app code, rerun the full suite and the Python compile check. Report any browser-only behavior that could not be exercised separately from passing server-side integration tests.
