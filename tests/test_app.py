from io import BytesIO
from datetime import datetime, timedelta, timezone
import json
import os
import re
import ssl
import tempfile
import time
import unittest
from pathlib import Path
from uuid import uuid4

import pg8000.dbapi
from PIL import Image

from app import create_app
from postgres_store import PostgresStore, _uuid
from store import FileStore
from werkzeug.security import check_password_hash, generate_password_hash


ROOT = Path(__file__).resolve().parents[1]


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = FileStore(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_update_export_delete_and_path_validation(self):
        record = {"id": "p1", "name": "Sample provider"}
        self.assertTrue(self.store.create("providers", "p1", record))
        self.assertFalse(self.store.create("providers", "p1", record))
        self.assertEqual(self.store.get("providers", "p1"), record)
        self.assertEqual(self.store.list("providers"), [record])
        self.assertEqual(self.store.export_all("providers"), [record])

        updated = {**record, "name": "Updated provider"}
        self.store.put("providers", "p1", updated)
        self.assertEqual(self.store.list("providers"), [updated])
        self.store.delete("providers", "p1")
        self.assertEqual(self.store.list("providers"), [])
        with self.assertRaises(ValueError):
            self.store.get("../outside", "record")
        with self.assertRaises(ValueError):
            self.store.put_config("../outside.json", {})


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _FakeConnection:
    """Stand-in for _Connection that counts opens without a live database."""

    PROVIDER_ROW = {
        "app_data": {},
        "id": "11111111-1111-1111-1111-111111111111",
        "owner_account_id": None,
        "name": "Test Salon",
        "category": "Salon",
        "tagline": "Test",
        "address": "1 Test Street",
        "status": "open",
        "public_email": "",
        "public_phone_e164": "",
        "approval_status": "approved",
        "approval_note": "",
        "approval_updated_at": None,
    }

    def __init__(self, fail_on=None):
        self.raw = type("Raw", (), {"_usock": object()})()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self.fail_on = fail_on
        self.executed = []

    def execute(self, query, params=None):
        self.executed.append(query)
        if self.fail_on and self.fail_on in query:
            raise pg8000.dbapi.InterfaceError("connection is closed")
        if query.strip().upper().startswith("SELECT 1"):
            return _FakeResult([{"?column?": 1}])
        if "FROM provider_services" in query:
            return _FakeResult([])
        if "FROM providers" in query:
            return _FakeResult([dict(self.PROVIDER_ROW)])
        return _FakeResult([])

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True
        self.raw._usock = None


class PostgresStoreTests(unittest.TestCase):
    def test_postgres_backend_is_opt_in_and_legacy_ids_map_stably_to_uuids(self):
        self.assertEqual(_uuid("legacy-provider-5"), _uuid("legacy-provider-5"))
        self.assertNotEqual(_uuid("legacy-provider-5"), _uuid("legacy-provider-6"))
        self.assertEqual(len(_uuid("legacy-provider-5")), 36)
        store = PostgresStore("postgresql://user:password@localhost/smartqueue")
        self.assertEqual(store.dsn, "postgresql://user:password@localhost/smartqueue")
        ssl_context = store._create_ssl_context("require", None)
        self.assertTrue(ssl_context.check_hostname)
        self.assertEqual(ssl_context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(
            PostgresStore(
                "postgresql://user:password@localhost/smartqueue",
                ssl_root_cert="root-ca.pem",
            ).ssl_root_cert
        )
        with self.assertRaises(ValueError):
            store._create_ssl_context("disable", None)
        with self.assertRaises(ValueError):
            PostgresStore("file:///tmp/store")

    def test_postgres_store_reuses_one_connection_across_operations(self):
        store = PostgresStore("postgresql://user:password@localhost/smartqueue")
        opened = []

        def opener():
            connection = _FakeConnection()
            opened.append(connection)
            return connection

        store._open_connection = opener
        self.assertEqual(len(store.list("providers")), 1)
        self.assertEqual(len(store.list("providers")), 1)
        self.assertEqual(
            store.get("providers", "11111111-1111-1111-1111-111111111111")["name"],
            "Test Salon",
        )
        with store.transaction():
            self.assertEqual(len(store.list("providers")), 1)
        self.assertEqual(len(opened), 1)
        self.assertFalse(opened[0].closed)
        self.assertGreaterEqual(opened[0].commits, 3)
        store.close()
        self.assertTrue(opened[0].closed)
        self.assertEqual(len(store.list("providers")), 1)
        self.assertEqual(len(opened), 2)

    def test_postgres_store_replaces_a_closed_connection(self):
        store = PostgresStore("postgresql://user:password@localhost/smartqueue")
        opened = []

        def opener():
            connection = _FakeConnection()
            opened.append(connection)
            return connection

        store._open_connection = opener
        self.assertEqual(len(store.list("providers")), 1)
        opened[0].raw._usock = None
        self.assertEqual(len(store.list("providers")), 1)
        self.assertEqual(len(opened), 2)
        self.assertTrue(opened[0].closed)

    def test_postgres_store_discards_broken_connection_and_probes_after_idle(self):
        store = PostgresStore("postgresql://user:password@localhost/smartqueue")
        opened = []

        def opener():
            connection = _FakeConnection()
            opened.append(connection)
            return connection

        store._open_connection = opener
        self.assertEqual(len(store.list("providers")), 1)
        # Simulate the pooler closing an idle connection: the next
        # operation must probe, replace it, and succeed.
        store._local.persistent_last_used = time.monotonic() - 3600
        opened[0].fail_on = "SELECT 1"
        self.assertEqual(len(store.list("providers")), 1)
        self.assertEqual(len(opened), 2)
        # A failure mid-operation discards the broken connection so the
        # following operation reconnects instead of reusing a dead socket.
        opened[1].fail_on = "FROM providers"
        with self.assertRaises(pg8000.dbapi.InterfaceError):
            store.list("providers")
        self.assertTrue(opened[1].closed)
        self.assertEqual(len(store.list("providers")), 1)
        self.assertEqual(len(opened), 3)

    def test_app_selects_postgres_only_when_database_url_is_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            app = create_app({
                "TESTING": True,
                "DATA_DIR": Path(temp_dir) / "store",
                "SEED_DIR": ROOT / "data" / "seed" / "providers",
                "DATABASE_URL": "postgresql://user:password@localhost/smartqueue",
            })
            self.assertTrue(app.config["POSTGRES_ENABLED"])
            self.assertIsInstance(app.extensions["smartqueue_store"], PostgresStore)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "DATA_DIR": Path(self.temp_dir.name) / "store",
            "SEED_DIR": ROOT / "data" / "seed" / "providers",
            "ADMIN_PASSWORD": "test-admin-password",
        })
        self.client = self.app.test_client()
        self.store = self.app.extensions["smartqueue_store"]
        response = self.client.get("/register")
        self.csrf_token = re.search(rb'name="csrf-token" content="([^"]+)"', response.data).group(1).decode()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = self.csrf_token

    def tearDown(self):
        self.temp_dir.cleanup()

    def create_account(self, role="customer", identifier="jamie@example.com", password="another-long-password"):
        data = {
            "name": "Jamie Rivera",
            "email": identifier if "@" in identifier else "",
            "phone": identifier if identifier.startswith("+") else "",
            "password": password,
            "confirm_password": password,
            "role": role,
        }
        if role == "provider":
            data.update({
                "business_name": "Jamie's Service",
                "category": "Salon",
                "address": "123 Main Street, Seattle",
                "services": "Cut, Color",
            })
        response = self.client.post("/register", data=data)
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        user = next(user for user in self.store.list("users") if user.get("email") == data["email"] or user.get("phone") == data["phone"])
        if role == "provider":
            provider_id = str(user["providerId"])
            provider = self.store.get("providers", provider_id)
            provider["approvalStatus"] = "approved"
            self.store.put("providers", provider_id, provider, provider)
        return user

    def login_account(self, identifier, password="another-long-password"):
        page = self.client.get("/login")
        token = re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
        response = self.client.post("/login", data={
            "identifier": identifier,
            "password": password,
            "csrf_token": token,
        })
        self.assertEqual(response.status_code, 303, response.get_data(as_text=True))
        with self.client.session_transaction() as browser_session:
            self.client.environ_base["HTTP_X_CSRF_TOKEN"] = browser_session["csrf_token"]
        return response

    def login_admin(self, password="test-admin-password"):
        page = self.client.get("/admin/login")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
        response = self.client.post("/admin/login", data={
            "password": password,
            "csrf_token": token,
        })
        expected_status = 303 if password == "test-admin-password" else 401
        self.assertEqual(response.status_code, expected_status, response.get_data(as_text=True))
        if response.status_code == 303:
            with self.client.session_transaction() as browser_session:
                self.client.environ_base["HTTP_X_CSRF_TOKEN"] = browser_session["csrf_token"]
        return response

    def test_seed_and_home_render(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Willow &amp; Sage", response.data)
        self.assertIn(b"Sign up", response.data)
        self.assertIn(b"\xc2\xa9 2026 NextInQueue", response.data)
        self.assertNotIn(b"\xc2\xa9 2025", response.data)
        dictionary = self.client.get("/static/i18n.js")
        self.assertEqual(dictionary.status_code, 200)
        self.assertIn(b"\xc2\xa9 2026 NextInQueue", dictionary.data)
        self.assertNotIn(b"\xc2\xa9 2025", dictionary.data)
        dictionary.close()
        self.assertEqual(len(self.store.list("providers")), 4)

    def test_provider_admin_and_static_pages_render(self):
        self.create_account("provider")
        self.login_account("jamie@example.com")
        provider = self.client.get("/?view=provider")
        admin = self.client.get("/?view=admin")
        script = self.client.get("/static/app.js")
        styles = self.client.get("/static/styles.css")
        self.assertEqual(provider.status_code, 200)
        self.assertIn(b"Here\xe2\x80\x99s how things are moving at Jamie&#39;s Service.", provider.data)
        self.assertIn(b"Start next", provider.data)
        self.assertIn(b"Add walk-in", provider.data)
        self.assertIn(b'id="toggle-presentation"', provider.data)
        self.assertIn(b"Expand display", provider.data)
        self.assertIn(b"Join your queue", provider.data)
        self.assertEqual(admin.status_code, 302)
        self.login_admin()
        admin = self.client.get("/?view=admin")
        self.assertEqual(admin.status_code, 200)
        self.assertIn(b"Provider approvals", admin.data)
        self.assertEqual(script.status_code, 200)
        self.assertEqual(styles.status_code, 200)
        script.close()
        styles.close()

    def test_language_picker_assets_are_served(self):
        for path in ("/", "/login", "/register", "/reset-password", "/admin/login"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn(b"/static/i18n.js", page.data, path)
        script = self.client.get("/static/i18n.js")
        self.assertEqual(script.status_code, 200)
        body = script.get_data(as_text=True)
        self.assertIn("smartqueue.lang", body)
        self.assertIn("कतार में शामिल हों", body)  # "Join queue" in Hindi
        self.assertIn("language-picker", body)
        script.close()

    def test_theme_picker_assets_are_served(self):
        for path in ("/", "/login", "/register", "/reset-password", "/admin/login"):
            page = self.client.get(path)
            self.assertEqual(page.status_code, 200, path)
            self.assertIn(b"/static/theme.js", page.data, path)
            self.assertIn(b"smartqueue.theme", page.data, path)
        script = self.client.get("/static/theme.js")
        self.assertEqual(script.status_code, 200)
        body = script.get_data(as_text=True)
        self.assertIn("smartqueue.theme", body)
        for theme_id in ("brand", "marigold", "sindoor", "rani", "peacock"):
            self.assertIn(theme_id, body)
        script.close()
        styles = self.client.get("/static/styles.css")
        css = styles.get_data(as_text=True)
        for theme_id in ("brand", "marigold", "sindoor", "rani", "peacock"):
            self.assertIn(f'[data-theme="{theme_id}"]', css)
        self.assertIn("--page-bg", css)
        styles.close()

    def test_logo_assets_are_served_and_used_in_header(self):
        home = self.client.get("/")
        self.assertIn(b"/branding/logo.png", home.data)
        self.assertIn(b"/branding/favicon.png", home.data)
        self.assertNotIn(b"brand-mark", home.data)
        for asset in ("logo.png", "logo-icon.png", "favicon.png"):
            response = self.client.get(f"/branding/{asset}")
            self.assertEqual(response.status_code, 200, asset)
            self.assertEqual(response.mimetype, "image/png", asset)
            self.assertTrue(response.data.startswith(b"\x89PNG"), asset)
            response.close()

    def test_new_provider_requires_approval_to_appear_or_accept_queue_entries(self):
        registration = self.client.post("/register", data={
            "name": "Pending Provider Owner",
            "email": "pending-owner@example.com",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "provider",
            "business_name": "Pending Sample Service",
            "category": "Healthcare",
            "address": "Mumbai, India",
            "services": "Consultation",
        })
        self.assertEqual(registration.status_code, 303)
        owner = next(user for user in self.store.list("users") if user.get("email") == "pending-owner@example.com")
        provider_id = str(owner["providerId"])
        provider = self.store.get("providers", provider_id)
        self.assertEqual(provider["approvalStatus"], "pending")

        listing = self.client.get("/")
        public_api = self.client.get("/api/providers")
        self.assertNotIn(b"Pending Sample Service", listing.data)
        self.assertNotIn(
            provider_id,
            [str(provider["id"]) for provider in json.loads(public_api.data)],
        )
        self.assertEqual(self.client.get(f"/providers/{provider_id}/qr.svg").status_code, 403)
        self.assertEqual(self.client.get(f"/providers/{provider_id}/walk-in").status_code, 403)

        self.create_account("customer", "approval-customer@example.com")
        self.login_account("approval-customer@example.com")
        not_approved = self.client.post("/api/queues", json={"providerId": provider_id})
        self.assertEqual(not_approved.status_code, 403)

        self.client.post("/logout")
        self.assertEqual(self.client.get("/?view=admin").status_code, 302)
        admin_login_page = self.client.get("/admin/login")
        admin_csrf = re.search(rb'name="csrf_token" value="([^"]+)"', admin_login_page.data).group(1).decode()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = admin_csrf
        self.assertEqual(
            self.client.patch(f"/api/admin/providers/{provider_id}/approval", json={"action": "approve"}).status_code,
            403,
        )
        self.login_admin(password="incorrect-admin-password")

    def test_admin_can_approve_reject_and_provider_can_request_rereview(self):
        provider_owner = self.create_account(
            "provider",
            "review-owner@example.com",
        )
        provider_id = str(provider_owner["providerId"])
        provider = self.store.get("providers", provider_id)
        provider["approvalStatus"] = "pending"
        self.store.put("providers", provider_id, provider, provider)

        self.assertEqual(self.client.get("/?view=admin").status_code, 302)
        login_page = self.client.get("/admin/login")
        self.assertEqual(login_page.status_code, 200)
        bad_login_token = re.search(rb'name="csrf_token" value="([^"]+)"', login_page.data).group(1).decode()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = bad_login_token
        self.assertEqual(self.client.patch(
            f"/api/admin/providers/{provider_id}/approval",
            json={"action": "approve"},
        ).status_code, 403)
        self.assertIn(b"Administrator password", login_page.data)
        bad_login = self.client.post("/admin/login", data={
            "password": "wrong-admin-password",
            "csrf_token": bad_login_token,
        })
        self.assertEqual(bad_login.status_code, 401)

        self.login_admin()
        dashboard = self.client.get("/?view=admin")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Jamie&#39;s Service", dashboard.data)
        approved = self.client.patch(
            f"/api/admin/providers/{provider_id}/approval",
            json={"action": "approve", "note": "Business details reviewed."},
        )
        self.assertEqual(approved.status_code, 200)
        self.assertEqual(approved.json["provider"]["approvalStatus"], "approved")
        self.assertEqual(self.store.list("provider_reviews")[-1]["decision"], "approved")
        self.assertIn(
            provider_id,
            [str(provider["id"]) for provider in self.client.get("/api/providers").json],
        )

        rejected = self.client.patch(
            f"/api/admin/providers/{provider_id}/approval",
            json={"action": "reject", "note": "Please verify your business address."},
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json["provider"]["approvalStatus"], "rejected")
        self.assertEqual(rejected.json["provider"]["status"], "Paused")
        self.assertNotIn(
            provider_id,
            [str(provider["id"]) for provider in self.client.get("/api/providers").json],
        )

        owner_client = self.app.test_client()
        owner_login_page = owner_client.get("/login")
        owner_csrf = re.search(rb'name="csrf_token" value="([^"]+)"', owner_login_page.data).group(1).decode()
        owner_client.post("/login", data={
            "identifier": "review-owner@example.com",
            "password": "another-long-password",
            "csrf_token": owner_csrf,
        })
        with owner_client.session_transaction() as browser_session:
            owner_client.environ_base["HTTP_X_CSRF_TOKEN"] = browser_session["csrf_token"]
        provider_dashboard = owner_client.get("/?view=provider")
        self.assertIn(b"Please verify your business address.", provider_dashboard.data)
        resubmitted = owner_client.post(f"/api/providers/{provider_id}/request-review")
        self.assertEqual(resubmitted.status_code, 200)
        self.assertEqual(resubmitted.json["provider"]["approvalStatus"], "pending")
        self.assertNotIn(
            provider_id,
            [str(provider["id"]) for provider in owner_client.get("/api/providers").json],
        )

    def test_provider_qr_and_live_queue_are_owner_scoped(self):
        owner = self.create_account("provider", "owner@example.com")
        other_owner = self.create_account("provider", "other-owner@example.com")
        self.login_account("owner@example.com")

        qr = self.client.get(f"/providers/{owner['providerId']}/qr.svg")
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.mimetype, "image/svg+xml")
        self.assertIn(b"<svg", qr.data)
        self.assertEqual(
            self.client.get(f"/api/providers/{owner['providerId']}/queues").json,
            {"queues": []},
        )

        self.login_account("other-owner@example.com")
        self.assertEqual(self.client.get(f"/api/providers/{owner['providerId']}/queues").status_code, 403)

    def test_public_walk_in_form_and_qr_join_without_account(self):
        provider_user = self.create_account("provider", "public-walkin-owner@example.com")
        provider_id = str(provider_user["providerId"])
        public_client = self.app.test_client()

        qr = public_client.get(f"/providers/{provider_id}/qr.svg")
        form = public_client.get(f"/providers/{provider_id}/walk-in")
        self.assertEqual(qr.status_code, 200)
        self.assertEqual(qr.mimetype, "image/svg+xml")
        self.assertEqual(form.status_code, 200)
        self.assertIn(b"Full name", form.data)
        self.assertIn(b"Your contact information is shared only with this provider", form.data)
        token = re.search(rb'name="csrf_token" value="([^"]+)"', form.data).group(1).decode()

        submitted = public_client.post(f"/providers/{provider_id}/walk-in", data={
            "csrf_token": token,
            "name": "Public Walk-in",
            "phone": "+1 206 555 0145",
            "service": "Cut",
        })
        self.assertEqual(submitted.status_code, 303)
        self.assertIn("joined=1", submitted.headers["Location"])
        confirmation = public_client.get(submitted.headers["Location"])
        self.assertIn(b"You\xe2\x80\x99re on the list", confirmation.data)
        queue = self.store.list("queues")[0]
        self.assertEqual(queue["customerId"], None)
        self.assertEqual(queue["phone"], "+12065550145")
        self.assertEqual(queue["source"], "walk-in")
        self.assertEqual(public_client.get("/api/providers").status_code, 200)
        self.assertNotIn(b"+12065550145", public_client.get("/api/providers").data)
        self.assertEqual(public_client.get(f"/api/providers/{provider_id}/queues").status_code, 401)

    def test_customer_join_queue_with_service_selection_and_default(self):
        provider_user = self.create_account("provider", "services-owner@example.com")
        provider_id = str(provider_user["providerId"])
        self.create_account("customer", "services-customer@example.com")
        self.login_account("services-customer@example.com")

        joined = self.client.post("/api/queues", json={
            "providerId": provider_id,
            "services": ["Color", "Cut"],
        })
        self.assertEqual(joined.status_code, 201)
        self.assertEqual(joined.json["queue"]["service"], "Color, Cut")

        self.assertEqual(self.client.delete(f"/api/queues/{joined.json['queue']['id']}").status_code, 200)
        rejoined = self.client.post("/api/queues", json={"providerId": provider_id, "services": []})
        self.assertEqual(rejoined.status_code, 201)
        self.assertEqual(rejoined.json["queue"]["service"], "Cut")

        self.assertEqual(self.client.delete(f"/api/queues/{rejoined.json['queue']['id']}").status_code, 200)
        filtered = self.client.post("/api/queues", json={
            "providerId": provider_id,
            "services": ["Not offered", "Color"],
        })
        self.assertEqual(filtered.status_code, 201)
        self.assertEqual(filtered.json["queue"]["service"], "Color")

    def test_customer_can_join_multiple_queues_and_leave_one(self):
        self.create_account()
        self.login_account("jamie@example.com")
        first = self.client.post("/api/queues", json={"providerId": 1, "services": ["Haircut"]})
        second = self.client.post("/api/queues", json={"providerId": 3})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(self.store.get("providers", "1")["people"], 1)
        self.assertEqual(self.store.get("providers", "3")["people"], 1)

        current = self.client.get("/api/queues/current")
        self.assertEqual(len(current.json["queues"]), 2)

        landing = self.client.get("/")
        self.assertEqual(landing.data.count(b"Leave queue"), 2)
        self.assertIn(b"Willow &amp; Sage", landing.data)
        self.assertIn(b"Little Bird Coffee", landing.data)
        self.assertIn(b"Haircut", landing.data)

        left = self.client.delete(f"/api/queues/{first.json['queue']['id']}")
        self.assertEqual(left.status_code, 200)
        self.assertIsNone(self.store.get("queues", first.json["queue"]["id"]))
        self.assertIsNotNone(self.store.get("queues", second.json["queue"]["id"]))
        current = self.client.get("/api/queues/current")
        self.assertEqual(
            [item["providerId"] for item in current.json["queues"]],
            [second.json["queue"]["providerId"]],
        )

    def test_queue_events_and_errors_are_logged(self):
        self.create_account()
        self.login_account("jamie@example.com")
        with self.assertLogs("smartqueue", level="INFO") as captured:
            joined = self.client.post("/api/queues", json={"providerId": 1, "services": ["Haircut"]})
            self.assertEqual(joined.status_code, 201)
            left = self.client.delete(f"/api/queues/{joined.json['queue']['id']}")
            self.assertEqual(left.status_code, 200)
            rejected = self.client.post("/api/queues", json={"providerId": 999})
            self.assertEqual(rejected.status_code, 404)
        messages = [record.getMessage() for record in captured.records]
        self.assertTrue(any(message.startswith("queue.join") for message in messages))
        self.assertTrue(any(message.startswith("queue.leave") for message in messages))
        self.assertTrue(any(message.startswith("api error status=404") for message in messages))

    def test_log_file_is_written_next_to_data_dir(self):
        log_path = Path(self.temp_dir.name) / "logs" / "smartqueue.log"
        self.assertTrue(log_path.exists())
        contents = log_path.read_text(encoding="utf-8")
        self.assertIn("SmartQueue app created: store=json-file", contents)

    def test_header_shows_signed_in_user_and_admin_link(self):
        signed_out = self.client.get("/")
        self.assertNotIn(b"signed-in-chip", signed_out.data)
        self.assertIn(b">Admin<", signed_out.data)
        self.assertNotIn(b"avatar-button", signed_out.data)

        self.create_account()
        self.login_account("jamie@example.com")
        landing = self.client.get("/")
        self.assertIn(b"signed-in-chip", landing.data)
        self.assertIn(b"Jamie Rivera", landing.data)
        self.assertIn(b">JR<", landing.data)
        self.assertIn(b">Admin<", landing.data)
        self.assertNotIn(b"avatar-button", landing.data)

    def test_header_chip_survives_a_missing_user_name(self):
        user = self.create_account()
        record = self.store.get("users", user["id"])
        record["name"] = ""
        index_entry = {key: value for key, value in record.items() if key != "passwordHash"}
        self.store.put("users", user["id"], record, index_entry)
        self.login_account("jamie@example.com")
        landing = self.client.get("/")
        self.assertEqual(landing.status_code, 200)
        self.assertIn(b"signed-in-chip", landing.data)
        self.assertIn(b"Guest", landing.data)

    def test_mobile_layout_rules_are_in_place(self):
        styles = self.client.get("/static/styles.css").get_data(as_text=True)
        self.assertIn(".header-inner { width: calc(100% - 24px); flex-wrap: wrap;", styles)
        self.assertIn(".header-actions { flex-basis: 100%; flex-wrap: wrap;", styles)
        self.assertIn(".theme-picker, .language-picker { font-size: 11px;", styles)
        self.assertIn(".customer-cell small { font-size: 9.5px; }", styles)

    def test_tagline_hero_card_and_info_pages(self):
        home = self.client.get("/")
        self.assertIn(b"Your place in line. Without the wait.", home.data)
        self.assertIn(b"<em>Without the wait.</em>", home.data)
        self.assertNotIn(b"Your time, better spent", home.data)
        self.assertNotIn(b"YOUR SPOT IS SAVED", home.data)
        self.assertIn(b"how-diagram", home.data)
        self.assertIn(b"Scan or tap", home.data)
        self.assertIn(b'href="/contact"', home.data)
        self.assertIn(b'href="/privacy-terms"', home.data)

        contact = self.client.get("/contact")
        self.assertEqual(contact.status_code, 200)
        self.assertIn(b"info@nextinqueue.in", contact.data)
        self.assertIn(b"Ward No 37", contact.data)
        self.assertIn(b"Bettiah, Pin - 845438", contact.data)

        legal = self.client.get("/privacy-terms")
        self.assertEqual(legal.status_code, 200)
        self.assertIn(b"Privacy Policy", legal.data)
        self.assertIn(b"Terms of Service", legal.data)
        self.assertIn(b"Digital Personal Data Protection Act, 2023", legal.data)

    def test_current_queues_skips_entries_whose_provider_is_gone(self):
        self.create_account()
        self.login_account("jamie@example.com")
        joined = self.client.post("/api/queues", json={"providerId": 1})
        self.assertEqual(joined.status_code, 201)
        self.store.delete("providers", "1")
        current = self.client.get("/api/queues/current")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json["queues"], [])

    def test_join_conflict_is_logged_with_a_clear_message(self):
        self.create_account()
        self.login_account("jamie@example.com")
        original_create = self.store.create

        def failing_create(collection, *args, **kwargs):
            if collection == "queues":
                return False
            return original_create(collection, *args, **kwargs)

        self.store.create = failing_create
        try:
            with self.assertLogs("smartqueue", level="WARNING") as captured:
                response = self.client.post("/api/queues", json={"providerId": 1})
        finally:
            self.store.create = original_create
        self.assertEqual(response.status_code, 409)
        self.assertIn("couldn't add you", response.json["error"])
        messages = [record.getMessage() for record in captured.records]
        self.assertTrue(any(message.startswith("queue.join_conflict") for message in messages))

    def test_provider_dashboard_shows_booked_services_and_view_options(self):
        provider_user = self.create_account("provider", "view-owner@example.com")
        provider_id = str(provider_user["providerId"])
        self.create_account("customer", "view-customer@example.com")
        self.login_account("view-customer@example.com")
        joined = self.client.post("/api/queues", json={
            "providerId": provider_id,
            "services": ["Color", "Cut"],
        })
        self.assertEqual(joined.status_code, 201)

        self.login_account("view-owner@example.com")
        queues = self.client.get(f"/api/providers/{provider_id}/queues")
        self.assertEqual(queues.json["queues"][0]["customerService"], "Color, Cut")
        dashboard = self.client.get("/?view=provider")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"heading-service", dashboard.data)
        self.assertIn(b'class="service-pill"', dashboard.data)
        self.assertIn(b"queue-service-filter", dashboard.data)
        self.assertIn(b"data-view-toggle", dashboard.data)

    def test_public_walk_in_multiple_services_and_default(self):
        provider_user = self.create_account("provider", "multi-walkin-owner@example.com")
        provider_id = str(provider_user["providerId"])
        public_client = self.app.test_client()

        form = public_client.get(f"/providers/{provider_id}/walk-in")
        self.assertIn(b'name="services"', form.data)
        token = re.search(rb'name="csrf_token" value="([^"]+)"', form.data).group(1).decode()
        submitted = public_client.post(f"/providers/{provider_id}/walk-in", data={
            "csrf_token": token,
            "name": "Multi Service",
            "phone": "+1 206 555 0146",
            "services": ["Cut", "Color"],
        })
        self.assertEqual(submitted.status_code, 303)
        self.assertEqual(self.store.list("queues")[0]["service"], "Cut, Color")

        form = public_client.get(f"/providers/{provider_id}/walk-in")
        token = re.search(rb'name="csrf_token" value="([^"]+)"', form.data).group(1).decode()
        submitted = public_client.post(f"/providers/{provider_id}/walk-in", data={
            "csrf_token": token,
            "name": "No Selection",
            "phone": "+1 206 555 0147",
        })
        self.assertEqual(submitted.status_code, 303)
        self.assertEqual(self.store.list("queues")[1]["service"], "Cut")

    def test_customer_registration_persists_hashed_password(self):
        page = self.client.get("/register")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Create your account", page.data)

        response = self.client.post("/register", data={
            "name": "Jamie Rivera",
            "email": "Jamie@example.com",
            "password": "a-long-test-password",
            "confirm_password": "a-long-test-password",
            "role": "customer",
        })
        self.assertEqual(response.status_code, 303)
        self.assertIn("created=1", response.headers["Location"])

        users = self.store.list("users")
        self.assertEqual(len(users), 1)
        self.assertEqual(users[0]["email"], "jamie@example.com")
        self.assertNotIn("passwordHash", users[0])
        record = self.store.get("users", users[0]["id"])
        self.assertNotEqual(record["passwordHash"], "a-long-test-password")
        self.assertTrue(check_password_hash(record["passwordHash"], "a-long-test-password"))
        self.assertNotIn("a-long-test-password", json.dumps(self.store.export_all("users")))

    def test_registration_rejects_mismatched_password_confirmation(self):
        response = self.client.post("/register", data={
            "name": "Jamie Rivera",
            "email": "jamie@example.com",
            "password": "a-long-test-password",
            "confirm_password": "a-different-long-password",
            "role": "customer",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"The passwords do not match", response.data)
        self.assertEqual(self.store.list("users"), [])

    def test_provider_registration_creates_provider_listing(self):
        response = self.client.post("/register", data={
            "name": "Taylor Lee",
            "email": "taylor@example.com",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "provider",
            "business_name": "Taylor's Salon",
            "category": "Salon",
            "address": "123 Main Street, Seattle",
            "services": "Cut, Color",
        })
        self.assertEqual(response.status_code, 303)

        user = self.store.list("users")[0]
        self.assertEqual(user["role"], "provider")
        provider = self.store.get("providers", str(user["providerId"]))
        self.assertEqual(provider["ownerId"], user["id"])
        self.assertEqual(provider["name"], "Taylor's Salon")
        self.assertEqual(provider["services"], ["Cut", "Color"])
        self.assertEqual(provider["people"], 0)
        self.assertEqual(provider["contactEmail"], "taylor@example.com")
        self.assertIsNone(provider["contactPhone"])
        self.login_admin()
        approval = self.client.patch(
            f"/api/admin/providers/{provider['id']}/approval",
            json={"action": "approve"},
        )
        self.assertEqual(approval.status_code, 200)
        public_provider = next(
            item for item in self.client.get("/api/providers").json
            if item["id"] == provider["id"]
        )
        self.assertEqual(public_provider["contactEmail"], "taylor@example.com")
        self.assertIsNone(public_provider["contactPhone"])

        self.login_account("taylor@example.com")
        listing = self.client.get("/")
        self.assertIn(b'mailto:taylor@example.com', listing.data)
        self.assertIn(b'taylor@example.com</a>', listing.data)
        dashboard = self.client.get("/?view=provider")
        self.assertIn(b'name="contactEmail"', dashboard.data)
        self.assertIn(b'value="taylor@example.com"', dashboard.data)

    def test_registered_healthcare_provider_is_discoverable_and_joinable_outside_default_location(self):
        response = self.client.post("/register", data={
            "name": "Sam Provider",
            "email": "sam@example.com",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "provider",
            "business_name": "Sample provider",
            "category": "Healthcare",
            "address": "44 Oak Road, Portland, OR",
            "services": "Consultation, Follow-up",
        })
        self.assertEqual(response.status_code, 303)
        provider = self.store.list("providers")[-1]
        self.assertEqual(provider["name"], "Sample provider")
        self.assertEqual(provider["category"], "Healthcare")
        self.assertEqual(self.store.get("providers", str(provider["id"])), provider)
        self.assertNotIn(
            provider["id"],
            [item["id"] for item in self.client.get("/api/providers").json],
        )

        self.login_admin()
        approval = self.client.patch(
            f"/api/admin/providers/{provider['id']}/approval",
            json={"action": "approve"},
        )
        self.assertEqual(approval.status_code, 200)
        public_provider = next(
            item for item in self.client.get("/api/providers").json
            if str(item["id"]) == str(provider["id"])
        )
        self.assertEqual(public_provider["name"], "Sample provider")
        self.assertEqual(public_provider["category"], "Healthcare")

        self.create_account("customer", "patient@example.com")
        self.login_account("patient@example.com")
        discovery = self.client.get("/")
        self.assertEqual(discovery.status_code, 200)
        self.assertIn(b'data-category="Healthcare"', discovery.data)
        self.assertIn(b"Sample provider", discovery.data)
        self.assertIn(b'id="location-input" aria-label="Location" placeholder="City, state, or ZIP" value=""', discovery.data)

        joined = self.client.post("/api/queues", json={"providerId": provider["id"]})
        self.assertEqual(joined.status_code, 201)
        self.assertEqual(joined.json["queue"]["providerId"], provider["id"])
        self.assertEqual(self.store.get("providers", str(provider["id"]))["people"], 1)

    def test_registration_validation_preserves_fields_and_uploaded_photo_for_retry(self):
        image_buffer = BytesIO()
        Image.new("RGB", (40, 30), color=(40, 100, 60)).save(image_buffer, format="PNG")
        image_buffer.seek(0)
        details = {
            "name": "Taylor Lee",
            "email": "taylor@example.com",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "provider",
            "business_name": "Taylor's Salon",
            "category": "Salon",
            "address": "",
            "services": "Cut, Color",
            "photo": (image_buffer, "salon.png", "image/png"),
        }
        invalid = self.client.post("/register", data=details, content_type="multipart/form-data")
        self.assertEqual(invalid.status_code, 400)
        self.assertIn(b'value="Taylor Lee"', invalid.data)
        self.assertIn(b'value="taylor@example.com"', invalid.data)
        self.assertIn(b'value="Taylor&#39;s Salon"', invalid.data)
        self.assertIn(b'value="Cut, Color"', invalid.data)
        self.assertNotIn(b"another-long-password", invalid.data)
        self.assertIn(b"Your selected photo is saved", invalid.data)
        self.assertEqual(len(list((Path(self.app.config["DATA_DIR"]) / "registration_drafts").glob("*.jpg"))), 1)

        details["address"] = "123 Main Street, Seattle"
        details.pop("photo")
        retried = self.client.post("/register", data=details)
        self.assertEqual(retried.status_code, 303)
        user = self.store.list("users")[0]
        provider = self.store.get("providers", str(user["providerId"]))
        self.assertTrue(user.get("photoId"))
        self.assertEqual(provider["photoId"], user["photoId"])
        self.assertTrue((Path(self.app.config["DATA_DIR"]) / "provider_photos" / user["photoId"]).is_file())
        self.assertEqual(list((Path(self.app.config["DATA_DIR"]) / "registration_drafts").glob("*.jpg")), [])

    def test_registration_discards_expired_staged_photos(self):
        draft_dir = Path(self.app.config["DATA_DIR"]) / "registration_drafts"
        draft_dir.mkdir(parents=True)
        draft_path = draft_dir / f"{'a' * 32}.jpg"
        draft_path.write_bytes(b"expired")
        os.utime(draft_path, (0, 0))
        with self.client.session_transaction() as browser_session:
            browser_session["registration_photo_draft"] = draft_path.name

        page = self.client.get("/register")

        self.assertEqual(page.status_code, 200)
        self.assertFalse(draft_path.exists())
        self.assertNotIn(b"Your selected photo is saved", page.data)
        with self.client.session_transaction() as browser_session:
            self.assertNotIn("registration_photo_draft", browser_session)

    def test_phone_only_registration_and_duplicate_phone(self):
        response = self.client.post("/register", data={
            "name": "Alex Customer",
            "phone": "+1 (206) 555-0134",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "customer",
        })
        self.assertEqual(response.status_code, 303)
        user = self.store.list("users")[0]
        self.assertIsNone(user["email"])
        self.assertEqual(user["phone"], "+12065550134")

        duplicate = self.client.post("/register", data={
            "name": "Another Customer",
            "phone": "+12065550134",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "customer",
        })
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn(b"phone number already exists", duplicate.data)
        self.assertEqual(len(self.store.list("users")), 1)

    def test_registration_accepts_email_and_phone_together(self):
        response = self.client.post("/register", data={
            "name": "Jamie Rivera",
            "email": "jamie@example.com",
            "phone": "+1 206 555 0134",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "customer",
        })
        self.assertEqual(response.status_code, 303)
        user = self.store.list("users")[0]
        self.assertEqual(user["email"], "jamie@example.com")
        self.assertEqual(user["phone"], "+12065550134")

    def test_provider_photo_is_saved_and_displayed_on_business_card(self):
        image_buffer = BytesIO()
        Image.new("RGB", (40, 30), color=(40, 100, 60)).save(image_buffer, format="PNG")
        image_buffer.seek(0)
        response = self.client.post("/register", data={
            "name": "Taylor Lee",
            "email": "taylor@example.com",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "provider",
            "business_name": "Taylor's Salon",
            "category": "Salon",
            "address": "123 Main Street, Seattle",
            "services": "Cut, Color",
            "photo": (image_buffer, "salon.png", "image/png"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 303)

        user = self.store.list("users")[0]
        provider = self.store.get("providers", str(user["providerId"]))
        self.assertEqual(provider["photoId"], user["photoId"])
        provider["approvalStatus"] = "approved"
        self.store.put("providers", str(provider["id"]), provider, provider)
        photo = self.client.get(f"/media/providers/{provider['photoId']}")
        self.assertEqual(photo.status_code, 200)
        self.assertEqual(photo.mimetype, "image/jpeg")
        self.assertEqual(photo.headers["X-Content-Type-Options"], "nosniff")
        photo.get_data()
        photo.close()
        card = self.client.get("/")
        self.assertIn(provider["photoId"].encode(), card.data)
        self.assertIn(b"provider-photo", card.data)

    def test_customer_photo_is_saved_without_public_media_route(self):
        image_buffer = BytesIO()
        Image.new("RGB", (20, 20), color=(70, 90, 110)).save(image_buffer, format="JPEG")
        image_buffer.seek(0)
        response = self.client.post("/register", data={
            "name": "Jamie Rivera",
            "phone": "+12065550134",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "customer",
            "photo": (image_buffer, "profile.jpg", "image/jpeg"),
        }, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 303)
        user = self.store.list("users")[0]
        photo_path = Path(self.app.config["DATA_DIR"]) / "customer_photos" / user["photoId"]
        self.assertTrue(photo_path.is_file())
        self.assertEqual(self.client.get(f"/media/providers/{user['photoId']}").status_code, 404)

    def test_registration_rejects_invalid_phone_and_non_image_upload(self):
        invalid_phone = self.client.post("/register", data={
            "name": "Jamie Rivera",
            "phone": "12345",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "customer",
        })
        self.assertEqual(invalid_phone.status_code, 400)
        self.assertIn(b"country code", invalid_phone.data)

        invalid_image = self.client.post("/register", data={
            "name": "Jamie Rivera",
            "email": "jamie@example.com",
            "password": "another-long-password",
            "role": "customer",
            "photo": (BytesIO(b"not an image"), "profile.png", "image/png"),
        }, content_type="multipart/form-data")
        self.assertEqual(invalid_image.status_code, 400)
        self.assertIn(b"not a valid supported image", invalid_image.data)

    def test_registration_rejects_invalid_fields_and_duplicate_email(self):
        invalid = self.client.post("/register", data={
            "name": "Taylor",
            "email": "not-an-email",
            "password": "short",
            "role": "admin",
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.store.list("users"), [])

        valid = {
            "name": "Taylor",
            "email": "taylor@example.com",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "customer",
        }
        self.assertEqual(self.client.post("/register", data=valid).status_code, 303)
        duplicate = self.client.post("/register", data={**valid, "email": "TAYLOR@example.com"})
        self.assertEqual(duplicate.status_code, 409)
        self.assertIn(b"already exists", duplicate.data)
        self.assertEqual(len(self.store.list("users")), 1)

    def test_provider_registration_requires_business_details(self):
        response = self.client.post("/register", data={
            "name": "Taylor",
            "email": "taylor@example.com",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "provider",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.store.list("users"), [])
        self.assertEqual(len(self.store.list("providers")), 4)

    def test_registration_rejects_services_that_settings_cannot_represent(self):
        response = self.client.post("/register", data={
            "name": "Taylor",
            "email": "taylor@example.com",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
            "role": "provider",
            "business_name": "Taylor's Salon",
            "category": "Salon",
            "address": "123 Main Street, Seattle",
            "services": ",".join(["A" * 61] + ["Service"] * 20),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"up to 20 services", response.data)
        self.assertEqual(len(self.store.list("users")), 0)

    def test_join_queue_leave_and_prevent_paused_provider(self):
        self.create_account()
        self.login_account("jamie@example.com")
        joined = self.client.post("/api/queues", json={"providerId": 1})
        self.assertEqual(joined.status_code, 201)
        queue = joined.json["queue"]
        self.assertEqual(queue["peopleAhead"], 0)
        self.assertEqual(self.store.get("providers", "1")["people"], 1)

        current = self.client.get("/api/queues/current")
        self.assertEqual([item["id"] for item in current.json["queues"]], [queue["id"]])
        left = self.client.delete(f"/api/queues/{queue['id']}")
        self.assertEqual(left.status_code, 200)
        self.assertEqual(self.store.get("providers", "1")["people"], 0)
        self.assertIsNone(self.store.get("queues", queue["id"]))

        self.assertEqual(self.client.patch("/api/providers/1", json={"status": "Paused"}).status_code, 403)
        provider = self.store.get("providers", "1")
        provider["status"] = "Paused"
        self.store.put("providers", "1", provider, provider)
        rejected = self.client.post("/api/queues", json={"providerId": 1})
        self.assertEqual(rejected.status_code, 409)

    def test_provider_status_controls_customer_join_and_multiple_queues(self):
        first_provider = self.create_account("provider", "first-provider@example.com")
        second_provider = self.create_account("provider", "second-provider@example.com")
        self.create_account("customer", "queue-customer@example.com")

        self.login_account("first-provider@example.com")
        provider_id = str(first_provider["providerId"])
        paused = self.client.patch(f"/api/providers/{provider_id}", json={"status": "Paused"})
        self.assertEqual(paused.status_code, 200)
        self.assertEqual(self.store.get("providers", provider_id)["status"], "Paused")

        self.login_account("queue-customer@example.com")
        paused_dashboard = self.client.get("/")
        self.assertIn(b"Queue paused", paused_dashboard.data)
        self.assertEqual(
            self.client.post("/api/queues", json={"providerId": first_provider["providerId"]}).status_code,
            409,
        )
        second_join = self.client.post("/api/queues", json={"providerId": second_provider["providerId"]})
        self.assertEqual(second_join.status_code, 201)
        second_queue_id = second_join.json["queue"]["id"]

        self.login_account("first-provider@example.com")
        reopened = self.client.patch(f"/api/providers/{provider_id}", json={"status": "Open"})
        self.assertEqual(reopened.status_code, 200)
        self.assertEqual(self.store.get("providers", provider_id)["status"], "Open")

        self.login_account("queue-customer@example.com")
        first_join = self.client.post("/api/queues", json={"providerId": first_provider["providerId"]})
        self.assertEqual(first_join.status_code, 201)
        # Joining another provider no longer leaves the earlier queue: the
        # customer now waits in both at once.
        self.assertIsNotNone(self.store.get("queues", second_queue_id))
        self.assertEqual(self.store.get("providers", str(second_provider["providerId"]))["people"], 1)
        self.assertEqual(self.store.get("providers", provider_id)["people"], 1)
        current = self.client.get("/api/queues/current")
        self.assertEqual(
            {str(item["providerId"]) for item in current.json["queues"]},
            {str(first_provider["providerId"]), str(second_provider["providerId"])},
        )

    def test_call_next_serves_oldest_and_updates_provider(self):
        provider_user_id = str(uuid4())
        provider_user = {
            "id": provider_user_id,
            "name": "Taylor Provider",
            "email": "taylor-provider@example.com",
            "phone": None,
            "role": "provider",
            "providerId": 1,
            "passwordHash": generate_password_hash("another-long-password"),
        }
        self.store.create("users", provider_user_id, provider_user, {key: value for key, value in provider_user.items() if key != "passwordHash"})
        provider = {"id": 1, "ownerId": provider_user_id, "name": "Sample", "status": "Open", "people": 2, "wait": 30, "serviceDuration": 15}
        self.store.put("providers", "1", provider, provider)
        customer_ids = [str(uuid4()), str(uuid4())]
        for customer_id in customer_ids:
            customer = {"id": customer_id, "name": "Queue Customer", "role": "customer", "passwordHash": "unused"}
            self.store.create("users", customer_id, customer, {key: value for key, value in customer.items() if key != "passwordHash"})
        for queue_id, created_at in [("later", "2026-01-02T00:00:00+00:00"), ("first", "2026-01-01T00:00:00+00:00")]:
            queue = {
                "id": queue_id,
                "customerId": customer_ids[0 if queue_id == "later" else 1],
                "providerId": 1,
                "status": "waiting",
                "createdAt": created_at,
            }
            self.store.put("queues", queue_id, queue, queue)

        self.login_account("taylor-provider@example.com")
        response = self.client.post("/api/providers/1/call-next")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.get("queues", "first")["status"], "serving")
        self.assertEqual(self.store.get("queues", "later")["status"], "waiting")
        self.assertEqual(response.json["providers"][0]["people"], 2)

        completed = self.client.patch("/api/providers/1/queues/first", json={"action": "complete"})
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(self.store.get("queues", "first")["status"], "completed")
        self.assertEqual(self.store.get("providers", "1")["people"], 1)

    def test_customer_can_login_save_favorites_and_see_live_queue_estimate(self):
        self.create_account()
        self.login_account("JAMIE@example.com")
        favorited = self.client.post("/api/favorites/1")
        self.assertEqual(favorited.status_code, 200)
        self.assertEqual(favorited.json["favoriteProviderIds"], ["1"])

        joined = self.client.post("/api/queues", json={"providerId": 1})
        self.assertEqual(joined.status_code, 201)
        current = self.client.get("/api/queues/current")
        self.assertEqual(current.status_code, 200)
        self.assertEqual(current.json["queues"][0]["position"], 1)
        self.assertEqual(current.json["queues"][0]["wait"], 0)
        landing = self.client.get("/")
        self.assertIn(b"Favorites", landing.data)
        self.assertIn(b"aria-pressed=\"true\"", landing.data)

        removed = self.client.delete("/api/favorites/1")
        self.assertEqual(removed.json["favoriteProviderIds"], [])

    def test_provider_settings_are_owner_only_and_update_listing(self):
        user = self.create_account("provider")
        self.login_account("jamie@example.com")
        provider_id = str(user["providerId"])
        updated = self.client.patch(f"/api/providers/{provider_id}", json={
            "name": "Jamie Studio",
            "category": "Healthcare",
            "address": "500 Pine Street",
            "description": "Thoughtful neighborhood care, with less waiting.",
            "contactEmail": "contact@jamie.example",
            "contactPhone": "98765 43210",
            "services": ["Consultation", "Follow-up"],
            "hours": "Mon-Fri 8 AM-5 PM",
            "serviceDuration": 25,
        })
        self.assertEqual(updated.status_code, 200)
        provider = self.store.get("providers", provider_id)
        self.assertEqual(provider["name"], "Jamie Studio")
        self.assertEqual(provider["serviceDuration"], 25)
        self.assertEqual(provider["services"], ["Consultation", "Follow-up"])
        self.assertEqual(provider["hours"], "Mon-Fri 8 AM-5 PM")
        self.assertEqual(provider["description"], "Thoughtful neighborhood care, with less waiting.")
        self.assertEqual(provider["contactEmail"], "contact@jamie.example")
        self.assertEqual(provider["contactPhone"], "+919876543210")
        public_provider = next(
            item for item in self.client.get("/api/providers").json
            if str(item["id"]) == provider_id
        )
        self.assertEqual(public_provider["contactEmail"], "contact@jamie.example")
        self.assertEqual(public_provider["contactPhone"], "+919876543210")
        listing = self.client.get("/")
        self.assertIn(b'mailto:contact@jamie.example', listing.data)
        self.assertIn(b'tel:+919876543210', listing.data)
        self.assertIn(b"Thoughtful neighborhood care, with less waiting.", listing.data)
        dashboard = self.client.get("/?view=provider")
        self.assertIn(b'name="description"', dashboard.data)
        self.assertIn(b'Thoughtful neighborhood care, with less waiting.', dashboard.data)
        self.assertEqual(self.client.patch("/api/providers/1", json={"status": "Paused"}).status_code, 403)
        invalid = self.client.patch(f"/api/providers/{provider_id}", json={"serviceDuration": 181})
        self.assertEqual(invalid.status_code, 400)
        invalid_services = self.client.patch(f"/api/providers/{provider_id}", json={"services": [""]})
        self.assertEqual(invalid_services.status_code, 400)
        invalid_email = self.client.patch(f"/api/providers/{provider_id}", json={"contactEmail": "bad-email"})
        invalid_phone = self.client.patch(f"/api/providers/{provider_id}", json={"contactPhone": "555-0100"})
        invalid_description = self.client.patch(f"/api/providers/{provider_id}", json={"description": "x" * 161})
        self.assertEqual(invalid_email.status_code, 400)
        self.assertEqual(invalid_phone.status_code, 400)
        self.assertEqual(invalid_description.status_code, 400)

    def test_provider_can_set_time_per_service(self):
        user = self.create_account("provider")
        self.login_account("jamie@example.com")
        provider_id = str(user["providerId"])
        updated = self.client.patch(f"/api/providers/{provider_id}", json={
            "serviceDurations": {"Cut": 15, "Color": 45},
        })
        self.assertEqual(updated.status_code, 200)
        provider = self.store.get("providers", provider_id)
        # "Cut" matches the 15-minute average, so only "Color" needs storing.
        self.assertEqual(provider["serviceDurations"], {"Color": 45})
        dashboard = self.client.get("/?view=provider")
        self.assertIn(b"Time per service", dashboard.data)
        self.assertIn(b'data-service="Color"', dashboard.data)
        self.assertIn(b'value="45"', dashboard.data)
        for payload in (
            {"serviceDurations": {"Color": 0}},
            {"serviceDurations": {"Color": 181}},
            {"serviceDurations": {"Color": "45"}},
            {"serviceDurations": ["Color"]},
        ):
            self.assertEqual(
                self.client.patch(f"/api/providers/{provider_id}", json=payload).status_code,
                400,
                payload,
            )
        # Times for services that do not exist are dropped, not stored.
        self.client.patch(f"/api/providers/{provider_id}", json={"serviceDurations": {"Massage": 30}})
        self.assertNotIn("serviceDurations", self.store.get("providers", provider_id))
        # Removing a service drops its stored time.
        self.client.patch(f"/api/providers/{provider_id}", json={"serviceDurations": {"Color": 45}})
        self.client.patch(f"/api/providers/{provider_id}", json={"services": ["Cut"]})
        self.assertNotIn("serviceDurations", self.store.get("providers", provider_id))

    def test_wait_estimates_use_each_services_time(self):
        provider_user = self.create_account("provider", "salon@example.com")
        provider_id = str(provider_user["providerId"])
        self.login_account("salon@example.com")
        updated = self.client.patch(f"/api/providers/{provider_id}", json={
            "services": ["Haircut", "Facial", "Eyebrow"],
            "serviceDurations": {"Facial": 45, "Eyebrow": 5},
        })
        self.assertEqual(updated.status_code, 200)
        # Customers see the configured times before joining.
        walk_in = self.client.get(f"/providers/{provider_id}/walk-in")
        self.assertIn(b">45 min<", walk_in.data)
        self.assertIn(b">5 min<", walk_in.data)

        self.create_account("customer", "ana@example.com")
        self.login_account("ana@example.com")
        first = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Facial"]})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.json["queue"]["wait"], 0)
        listing = self.client.get("/")
        self.assertIn(b'data-minutes="45"', listing.data)

        self.create_account("customer", "ben@example.com")
        self.login_account("ben@example.com")
        second = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Haircut"]})
        self.assertEqual(second.status_code, 201)
        # Haircut falls back to the 15-minute average, but Ben waits behind
        # Ana's 45-minute Facial — not behind one 15-minute average slot.
        self.assertEqual(second.json["queue"]["wait"], 45)
        current = self.client.get("/api/queues/current")
        self.assertEqual(current.json["queues"][0]["wait"], 45)
        self.assertEqual(current.json["queues"][0]["peopleAhead"], 1)

        self.create_account("customer", "cyd@example.com")
        self.login_account("cyd@example.com")
        third = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Haircut", "Facial"]})
        self.assertEqual(third.status_code, 201)
        self.assertEqual(third.json["queue"]["wait"], 60)

        provider = self.store.get("providers", provider_id)
        self.assertEqual(provider["people"], 3)
        self.assertEqual(provider["wait"], 120)

        self.login_account("salon@example.com")
        queues = self.client.get(f"/api/providers/{provider_id}/queues?status=active").json["queues"]
        by_service = {entry["service"]: entry for entry in queues}
        self.assertEqual(by_service["Facial"]["duration"], 45)
        self.assertEqual(by_service["Facial"]["estimatedWait"], 0)
        self.assertEqual(by_service["Haircut"]["duration"], 15)
        self.assertEqual(by_service["Haircut"]["estimatedWait"], 45)
        self.assertEqual(by_service["Haircut, Facial"]["duration"], 60)
        self.assertEqual(by_service["Haircut, Facial"]["estimatedWait"], 60)

    def test_provider_can_update_listing_photo_and_only_owner_can_upload(self):
        owner = self.create_account("provider", "photo-owner@example.com")
        other_owner = self.create_account("provider", "photo-other@example.com")
        provider_id = str(owner["providerId"])
        self.login_account("photo-owner@example.com")
        page = self.client.get("/?view=provider")
        self.assertIn(b'name="description"', page.data)
        self.assertIn(b'id="provider-photo-form"', page.data)
        self.assertIn(b'name="contactEmail"', page.data)
        self.assertIn(b'name="contactPhone"', page.data)
        self.assertIn(b'name="services"', page.data)
        self.assertIn(b'name="hours"', page.data)
        self.assertIn(b'name="serviceDuration"', page.data)

        image_buffer = BytesIO()
        Image.new("RGB", (40, 30), color=(40, 100, 60)).save(image_buffer, format="PNG")
        image_buffer.seek(0)
        uploaded = self.client.post(
            f"/api/providers/{provider_id}/photo",
            data={"photo": (image_buffer, "business.png", "image/png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.get_data(as_text=True))
        photo_id = uploaded.json["provider"]["photoId"]
        # Photos are stored in the database (provider_photos), not as
        # local files, so they survive hosts with ephemeral disks.
        self.assertIsNotNone(self.store.get("provider_photos", photo_id.removesuffix(".jpg")))
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn(photo_id.encode(), self.client.get("/").data)

        invalid = self.client.post(
            f"/api/providers/{provider_id}/photo",
            data={"photo": (BytesIO(b"not an image"), "business.png", "image/png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(self.store.get("providers", provider_id)["photoId"], photo_id)

        self.login_account("photo-other@example.com")
        image_buffer = BytesIO()
        Image.new("RGB", (20, 20), color=(70, 90, 110)).save(image_buffer, format="JPEG")
        image_buffer.seek(0)
        forbidden = self.client.post(
            f"/api/providers/{provider_id}/photo",
            data={"photo": (image_buffer, "other.jpg", "image/jpeg")},
            content_type="multipart/form-data",
        )
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(self.store.get("providers", provider_id)["photoId"], photo_id)

    def test_provider_cannot_manage_another_provider_or_queue(self):
        owner_one = self.create_account("provider", "one@example.com")
        owner_two = self.create_account("provider", "two@example.com")
        customer = self.create_account("customer", "queue-customer@example.com")
        self.login_account("queue-customer@example.com")
        joined = self.client.post("/api/queues", json={"providerId": owner_one["providerId"]})
        queue_id = joined.json["queue"]["id"]

        self.login_account("two@example.com")
        forbidden_settings = self.client.patch(f"/api/providers/{owner_one['providerId']}", json={"status": "Paused"})
        forbidden_queue = self.client.patch(
            f"/api/providers/{owner_one['providerId']}/queues/{queue_id}",
            json={"action": "serve"},
        )
        self.assertEqual(forbidden_settings.status_code, 403)
        self.assertEqual(forbidden_queue.status_code, 403)
        self.assertNotEqual(owner_one["providerId"], owner_two["providerId"])
        self.assertEqual(self.store.get("queues", queue_id)["status"], "waiting")

    def test_provider_adds_walkin_with_private_contact_and_owner_only_access(self):
        owner = self.create_account("provider", "walkin-owner@example.com")
        other_owner = self.create_account("provider", "walkin-other@example.com")
        self.login_account("walkin-owner@example.com")
        provider_id = str(owner["providerId"])

        added = self.client.post(f"/api/providers/{provider_id}/walk-ins", json={
            "name": "Walk-in Visitor",
            "email": "visitor@example.com",
            "phone": "98765 43210",
            "address": "7 Pine Street",
            "service": "Cut",
        })
        self.assertEqual(added.status_code, 201)
        queue = added.json["queue"]
        self.assertEqual(queue["status"], "waiting")
        self.assertEqual(queue["source"], "walk-in")
        self.assertEqual(queue["email"], "visitor@example.com")
        self.assertEqual(queue["phone"], "+919876543210")
        self.assertEqual(queue["address"], "7 Pine Street")
        self.assertEqual(queue["service"], "Cut")
        self.assertEqual(self.store.get("queues", queue["id"])["customerName"], "Walk-in Visitor")
        self.assertEqual(self.store.get("providers", provider_id)["people"], 1)

        private_queues = self.client.get(f"/api/providers/{provider_id}/queues")
        self.assertEqual(private_queues.status_code, 200)
        self.assertEqual(private_queues.json["queues"][0]["customerPhone"], "+919876543210")
        self.assertEqual(private_queues.json["queues"][0]["customerEmail"], "visitor@example.com")
        dashboard = self.client.get("/?view=provider")
        self.assertIn(b"Walk-in Visitor", dashboard.data)
        self.assertIn(b"visitor@example.com", dashboard.data)
        self.assertIn(b"+919876543210", dashboard.data)
        self.assertIn(b'data-queue-filter="completed"', dashboard.data)

        public_providers = self.client.get("/api/providers")
        self.assertNotIn(b"visitor@example.com", public_providers.data)
        self.assertNotIn(b"+919876543210", public_providers.data)
        discovery = self.client.get("/")
        self.assertNotIn(b"visitor@example.com", discovery.data)
        self.assertNotIn(b"+919876543210", discovery.data)

        invalid = self.client.post(f"/api/providers/{provider_id}/walk-ins", json={
            "name": "Missing contact",
            "service": "Cut",
        })
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("email address", invalid.json["error"])
        self.assertEqual(len(self.store.list("queues")), 1)

        self.login_account("walkin-other@example.com")
        forbidden_add = self.client.post(f"/api/providers/{provider_id}/walk-ins", json={
            "name": "Not authorized",
            "phone": "+12065550198",
            "service": "Cut",
        })
        forbidden_list = self.client.get(f"/api/providers/{provider_id}/queues")
        self.assertEqual(forbidden_add.status_code, 403)
        self.assertEqual(forbidden_list.status_code, 403)
        self.assertEqual(
            self.client.get(f"/api/providers/{provider_id}/queues?status=completed").status_code,
            403,
        )
        self.assertEqual(len(self.store.list("queues")), 1)

        self.login_account("walkin-owner@example.com")
        started = self.client.post(f"/api/providers/{provider_id}/call-next")
        self.assertEqual(started.status_code, 200)
        self.assertEqual(self.store.get("queues", queue["id"])["status"], "serving")
        completed = self.client.patch(
            f"/api/providers/{provider_id}/queues/{queue['id']}",
            json={"action": "complete"},
        )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(self.store.get("queues", queue["id"])["status"], "completed")
        self.assertEqual(self.store.get("providers", provider_id)["people"], 0)
        completed_history = self.client.get(f"/api/providers/{provider_id}/queues?status=completed")
        self.assertEqual(completed_history.status_code, 200)
        self.assertEqual([entry["id"] for entry in completed_history.json["queues"]], [queue["id"]])
        self.assertEqual(completed_history.json["queues"][0]["customerEmail"], "visitor@example.com")
        self.assertTrue(completed_history.json["queues"][0]["completedAt"])
        self.assertEqual(self.client.get(f"/api/providers/{provider_id}/queues").json["queues"], [])
        self.assertEqual(
            self.client.get(f"/api/providers/{provider_id}/queues?status=unknown").status_code,
            400,
        )

    def test_queue_service_transitions_reject_invalid_actions(self):
        provider_user = self.create_account("provider", "provider@example.com")
        self.create_account("customer", "first@example.com")
        self.create_account("customer", "second@example.com")
        provider_id = provider_user["providerId"]
        self.login_account("first@example.com")
        first = self.client.post("/api/queues", json={"providerId": provider_id}).json["queue"]
        self.login_account("second@example.com")
        second = self.client.post("/api/queues", json={"providerId": provider_id}).json["queue"]

        self.login_account("provider@example.com")
        out_of_order = self.client.patch(
            f"/api/providers/{provider_id}/queues/{second['id']}",
            json={"action": "serve"},
        )
        self.assertEqual(out_of_order.status_code, 200)
        already_serving = self.client.patch(
            f"/api/providers/{provider_id}/queues/{first['id']}",
            json={"action": "serve"},
        )
        complete_waiting = self.client.patch(
            f"/api/providers/{provider_id}/queues/{first['id']}",
            json={"action": "complete"},
        )
        self.assertEqual(already_serving.status_code, 409)
        self.assertEqual(complete_waiting.status_code, 409)
        self.assertEqual(self.store.get("queues", second["id"])["status"], "serving")
        self.assertEqual(self.store.get("queues", first["id"])["status"], "waiting")

    def test_wait_estimate_updates_after_provider_completes_service(self):
        provider_user = self.create_account("provider", "provider@example.com")
        provider_id = provider_user["providerId"]
        first_customer = self.create_account("customer", "first@example.com")
        second_customer = self.create_account("customer", "second@example.com")

        self.login_account("first@example.com")
        first_join = self.client.post("/api/queues", json={"providerId": provider_id})
        self.assertEqual(first_join.status_code, 201)
        self.login_account("second@example.com")
        second_join = self.client.post("/api/queues", json={"providerId": provider_id})
        self.assertEqual(second_join.status_code, 201)
        self.assertEqual(second_join.json["queue"]["wait"], 15)

        self.login_account("provider@example.com")
        serving = self.client.post(f"/api/providers/{provider_id}/call-next")
        self.assertEqual(serving.status_code, 200)
        first_queue_id = serving.json["queue"]["id"]
        self.assertEqual(serving.json["queue"]["customerId"], first_customer["id"])
        completed = self.client.patch(
            f"/api/providers/{provider_id}/queues/{first_queue_id}",
            json={"action": "complete"},
        )
        self.assertEqual(completed.status_code, 200)

        self.login_account("second@example.com")
        current = self.client.get("/api/queues/current")
        self.assertEqual(len(current.json["queues"]), 1)
        self.assertEqual(current.json["queues"][0]["customerId"], second_customer["id"])
        self.assertEqual(current.json["queues"][0]["position"], 1)
        self.assertEqual(current.json["queues"][0]["wait"], 0)

    def test_login_accepts_phone_and_bad_credentials_fail(self):
        self.create_account(identifier="+12065550134")
        self.login_account("+1 (206) 555-0134")
        bad = self.client.post("/login", data={
            "identifier": "+12065550134",
            "password": "not-the-password",
        })
        self.assertEqual(bad.status_code, 401)

    def test_indian_phone_defaults_apply_to_registration_login_and_password_reset(self):
        registered = self.client.post("/register", data={
            "name": "Asha Customer",
            "email": "asha@example.com",
            "phone": "98765 43210",
            "password": "a-long-test-password",
            "confirm_password": "a-long-test-password",
            "role": "customer",
        })
        self.assertEqual(registered.status_code, 303)
        account = self.store.list("users")[0]
        self.assertEqual(account["phone"], "+919876543210")

        self.login_account("09876543210", password="a-long-test-password")
        self.assertEqual(self.client.get("/").status_code, 200)

        self.client.post("/logout")
        reset_page = self.client.get("/reset-password")
        reset_token = re.search(rb'name="csrf-token" content="([^"]+)"', reset_page.data).group(1).decode()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = reset_token
        reset = self.client.post("/reset-password", data={
            "email": "asha@example.com",
            "phone": "9876543210",
            "password": "another-long-password",
            "confirm_password": "another-long-password",
        })
        self.assertEqual(reset.status_code, 200)
        self.assertTrue(check_password_hash(
            self.store.get("users", account["id"])["passwordHash"],
            "another-long-password",
        ))
        self.login_account("+91 98765 43210", password="another-long-password")

    def test_password_reset_requires_matching_contacts_and_updates_password(self):
        response = self.client.post("/register", data={
            "name": "Jamie Rivera",
            "email": "jamie@example.com",
            "phone": "+1 206 555 0134",
            "password": "old-long-password",
            "confirm_password": "old-long-password",
            "role": "customer",
        })
        self.assertEqual(response.status_code, 303)
        record = self.store.get("users", self.store.list("users")[0]["id"])
        old_hash = record["passwordHash"]

        page = self.client.get("/reset-password")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Reset password", page.data)
        self.assertIn(b"Account email", page.data)
        self.assertIn(b"Account phone number", page.data)

        mismatch = self.client.post("/reset-password", data={
            "email": "jamie@example.com",
            "phone": "+12065550135",
            "password": "new-long-password",
            "confirm_password": "new-long-password",
        })
        self.assertEqual(mismatch.status_code, 200)
        self.assertIn(b"If those contact details match an account", mismatch.data)
        self.assertEqual(self.store.get("users", record["id"])["passwordHash"], old_hash)

        reset = self.client.post("/reset-password", data={
            "email": "JAMIE@example.com",
            "phone": "+1 (206) 555-0134",
            "password": "new-long-password",
            "confirm_password": "new-long-password",
        })
        self.assertEqual(reset.status_code, 200)
        updated_record = self.store.get("users", record["id"])
        self.assertNotEqual(updated_record["passwordHash"], old_hash)
        self.assertTrue(check_password_hash(updated_record["passwordHash"], "new-long-password"))
        self.assertNotIn("passwordHash", self.store.list("users")[0])
        self.login_account("jamie@example.com", password="new-long-password")

    def test_password_reset_rejects_mismatched_confirmation(self):
        response = self.client.post("/reset-password", data={
            "email": "jamie@example.com",
            "phone": "+12065550134",
            "password": "new-long-password",
            "confirm_password": "another-long-password",
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn(b"The passwords do not match", response.data)
        self.assertEqual(self.store.list("users"), [])

    def test_login_csrf_failure_renders_login_form_and_success_rotates_token(self):
        self.create_account()
        page = self.client.get("/login")
        token = re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
        invalid = self.client.post(
            "/login",
            data={"identifier": "jamie@example.com", "password": "wrong"},
            headers={"X-CSRF-Token": "invalid-token"},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertIn(b"Email address or phone number", invalid.data)
        self.assertNotIn(b"Create your account", invalid.data)

        logged_in = self.client.post("/login", data={
            "identifier": "jamie@example.com",
            "password": "another-long-password",
            "csrf_token": token,
        })
        self.assertEqual(logged_in.status_code, 303)
        with self.client.session_transaction() as browser_session:
            self.assertNotEqual(browser_session["csrf_token"], token)
            self.assertTrue(browser_session.get("user_id"))

    def test_mutating_api_requires_csrf_token_and_signed_in_role(self):
        no_token = self.client.post("/api/queues", json={"providerId": 1}, headers={"X-CSRF-Token": ""})
        self.assertEqual(no_token.status_code, 400)
        self.assertEqual(no_token.json["error"], "The request token is missing or invalid. Refresh and retry.")

        not_logged_in = self.client.post("/api/queues", json={"providerId": 1})
        self.assertEqual(not_logged_in.status_code, 401)

    def test_logout_clears_session_and_provider_view_requires_login(self):
        self.create_account()
        self.login_account("jamie@example.com")
        signed_out = self.client.post("/logout")
        self.assertEqual(signed_out.status_code, 303)
        self.assertEqual(self.client.get("/?view=provider").status_code, 302)
        self.assertIn("/login", self.client.get("/?view=provider").headers["Location"])
        self.assertEqual(self.client.post("/logout").status_code, 400)

    def test_provider_page_uses_one_status_region_and_login_link_is_present(self):
        self.create_account("provider")
        self.login_account("jamie@example.com")
        page = self.client.get("/?view=provider")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.data.count(b'id="provider-settings-notice"'), 1)
        self.assertNotIn(b'id="provider-notice"', page.data)

        self.client.post("/logout")
        register = self.client.get("/register?created=1")
        self.assertIn(b"Log in to NextInQueue", register.data)
        self.assertNotIn(b"will be added separately", register.data)

    def relogin(self, identifier):
        # After a logout the session CSRF token is regenerated; refresh the
        # header token from a fresh page so it does not shadow the form token.
        page = self.client.get("/login")
        token = re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = token
        return self.login_account(identifier)

    def fresh_csrf(self):
        # After a logout the session has no CSRF token until a page is
        # fetched; sync the header token so form posts are accepted again.
        page = self.client.get("/register")
        token = re.search(rb'name="csrf-token" content="([^"]+)"', page.data).group(1).decode()
        self.client.environ_base["HTTP_X_CSRF_TOKEN"] = token

    def day_zone(self):
        # Pick a supported timezone whose local time is comfortably inside
        # one day, so open/close windows computed from "now" never cross
        # midnight during the test.
        from zoneinfo import ZoneInfo

        for name in ("Asia/Kolkata", "UTC", "Europe/London", "America/New_York", "America/Los_Angeles"):
            local_now = datetime.now(ZoneInfo(name))
            if 6 <= local_now.hour <= 16:
                return name, local_now
        raise AssertionError("No usable timezone found")

    def set_provider_hours(self, provider_id, **overrides):
        zone_name, local_now = self.day_zone()
        settings = {
            "openTime": (local_now - timedelta(hours=2)).strftime("%H:%M"),
            "closeTime": (local_now + timedelta(hours=2)).strftime("%H:%M"),
            "timezone": zone_name,
            "bufferMinutes": 60,
            "carryForwardQueue": False,
        }
        settings.update(overrides)
        return self.client.patch(f"/api/providers/{provider_id}", json=settings)

    def test_provider_day_settings_validation_and_dashboard(self):
        provider_user = self.create_account("provider", "hours-owner@example.com")
        provider_id = str(provider_user["providerId"])
        self.relogin("hours-owner@example.com")

        only_open = self.client.patch(f"/api/providers/{provider_id}", json={"openTime": "09:00"})
        self.assertEqual(only_open.status_code, 400)
        backwards = self.client.patch(
            f"/api/providers/{provider_id}",
            json={"openTime": "18:00", "closeTime": "09:00"},
        )
        self.assertEqual(backwards.status_code, 400)
        bad_buffer = self.client.patch(f"/api/providers/{provider_id}", json={"bufferMinutes": 500})
        self.assertEqual(bad_buffer.status_code, 400)

        saved = self.set_provider_hours(provider_id)
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        provider = self.store.get("providers", provider_id)
        self.assertEqual(provider["bufferMinutes"], 60)
        self.assertFalse(provider["carryForwardQueue"])
        self.assertIn("openTime", provider)

        page = self.client.get("/?view=provider")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Dequeued", page.data)
        self.assertIn(b'name="openTime"', page.data)
        self.assertIn(b'name="carryForwardQueue"', page.data)
        self.assertIn(b"PREDICTED", page.data)
        self.assertIn(b"data-i18n-name", page.data)

    def test_capacity_buffer_waitlist_and_promotion(self):
        provider_user = self.create_account("provider", "capacity-owner@example.com")
        provider_id = str(provider_user["providerId"])
        self.relogin("capacity-owner@example.com")
        self.assertEqual(
            self.client.patch(f"/api/providers/{provider_id}", json={"serviceDuration": 5}).status_code,
            200,
        )
        zone_name, local_now = self.day_zone()
        saved = self.client.patch(f"/api/providers/{provider_id}", json={
            "openTime": (local_now - timedelta(hours=1)).strftime("%H:%M"),
            "closeTime": (local_now + timedelta(minutes=13)).strftime("%H:%M"),
            "timezone": zone_name,
            "bufferMinutes": 0,
        })
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.client.post("/logout")

        queue_ids = []
        for index in range(3):
            email = f"capacity-customer-{index}@example.com"
            self.fresh_csrf()
            self.create_account("customer", email)
            self.relogin(email)
            joined = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Cut"]})
            self.assertEqual(joined.status_code, 201, joined.get_data(as_text=True))
            queue_ids.append(joined.json["queue"]["id"])
            if index < 2:
                self.assertFalse(joined.json["queue"]["buffer"])
            self.client.post("/logout")

        # Third join exceeds today's remaining minutes: buffer waitlist.
        third = self.store.get("queues", queue_ids[2])
        self.assertTrue(third["buffer"])
        self.assertIn("predictedTurnAt", third)

        self.relogin("capacity-customer-2@example.com")
        current = self.client.get("/api/queues/current")
        self.assertTrue(current.json["queues"][0]["buffer"])
        landing = self.client.get("/")
        self.assertIn(b"BUFFER WAITLIST", landing.data)
        self.assertIn(b"Predicted turn", landing.data)
        self.assertIn(b'id="refresh-my-queue"', landing.data)
        self.client.post("/logout")

        # Serving and completing the first customer frees capacity, and the
        # buffer entry is promoted into the regular queue.
        self.relogin("capacity-owner@example.com")
        served = self.client.patch(
            f"/api/providers/{provider_id}/queues/{queue_ids[0]}",
            json={"action": "serve"},
        )
        self.assertEqual(served.status_code, 200, served.get_data(as_text=True))
        completed = self.client.patch(
            f"/api/providers/{provider_id}/queues/{queue_ids[0]}",
            json={"action": "complete"},
        )
        self.assertEqual(completed.status_code, 200, completed.get_data(as_text=True))
        self.assertFalse(self.store.get("queues", queue_ids[2])["buffer"])

    def test_remove_restore_and_dequeued_tab(self):
        provider_user = self.create_account("provider", "remove-owner@example.com")
        provider_id = str(provider_user["providerId"])
        self.create_account("customer", "remove-customer@example.com")
        self.relogin("remove-customer@example.com")
        joined = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Cut"]})
        queue_id = joined.json["queue"]["id"]
        self.client.post("/logout")

        self.relogin("remove-owner@example.com")
        removed = self.client.patch(
            f"/api/providers/{provider_id}/queues/{queue_id}",
            json={"action": "remove", "reason": "no_show"},
        )
        self.assertEqual(removed.status_code, 200, removed.get_data(as_text=True))
        entry = self.store.get("queues", queue_id)
        self.assertEqual(entry["status"], "no_show")
        self.assertEqual(entry["removeReason"], "no_show")

        dequeued = self.client.get(f"/api/providers/{provider_id}/queues?status=dequeued")
        self.assertEqual([item["id"] for item in dequeued.json["queues"]], [queue_id])
        active = self.client.get(f"/api/providers/{provider_id}/queues?status=active")
        self.assertEqual(active.json["queues"], [])

        restored = self.client.patch(
            f"/api/providers/{provider_id}/queues/{queue_id}",
            json={"action": "restore"},
        )
        self.assertEqual(restored.status_code, 200, restored.get_data(as_text=True))
        self.assertEqual(self.store.get("queues", queue_id)["status"], "waiting")
        active = self.client.get(f"/api/providers/{provider_id}/queues?status=active")
        self.assertEqual([item["id"] for item in active.json["queues"]], [queue_id])

    def test_day_rollover_clears_or_carries_waiting_entries(self):
        from zoneinfo import ZoneInfo

        provider_user = self.create_account("provider", "rollover-owner@example.com")
        provider_id = str(provider_user["providerId"])
        self.relogin("rollover-owner@example.com")
        saved = self.set_provider_hours(provider_id, openTime="00:00", closeTime="23:59")
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        zone_name = self.store.get("providers", provider_id)["timezone"]
        self.client.post("/logout")

        self.fresh_csrf()
        self.create_account("customer", "rollover-customer@example.com")
        self.relogin("rollover-customer@example.com")
        joined = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Cut"]})
        queue_id = joined.json["queue"]["id"]
        self.client.post("/logout")

        # Age the entry to yesterday in the provider's timezone.
        entry = self.store.get("queues", queue_id)
        joined_local = datetime.now(ZoneInfo(zone_name)) - timedelta(days=1)
        entry["createdAt"] = joined_local.astimezone(timezone.utc).isoformat()
        self.store.put("queues", queue_id, entry, entry)

        self.relogin("rollover-owner@example.com")
        dequeued = self.client.get(f"/api/providers/{provider_id}/queues?status=dequeued")
        self.assertEqual([item["id"] for item in dequeued.json["queues"]], [queue_id])
        self.assertEqual(dequeued.json["queues"][0]["removeReason"], "day_end")

        # With carry-forward on, a stale entry stays in the queue, flagged.
        restored = self.client.patch(
            f"/api/providers/{provider_id}/queues/{queue_id}",
            json={"action": "restore"},
        )
        self.assertEqual(restored.status_code, 200)
        entry = self.store.get("queues", queue_id)
        entry["createdAt"] = joined_local.astimezone(timezone.utc).isoformat()
        self.store.put("queues", queue_id, entry, entry)
        self.assertEqual(
            self.client.patch(
                f"/api/providers/{provider_id}",
                json={"carryForwardQueue": True},
            ).status_code,
            200,
        )
        active = self.client.get(f"/api/providers/{provider_id}/queues?status=active")
        self.assertEqual([item["id"] for item in active.json["queues"]], [queue_id])
        self.assertTrue(active.json["queues"][0]["carriedOver"])

    def test_measured_service_times_drive_predictions_and_are_logged(self):
        provider_user = self.create_account("provider", "stats-owner@example.com")
        provider_id = str(provider_user["providerId"])
        self.create_account("customer", "stats-customer@example.com")

        for _ in range(3):
            self.relogin("stats-customer@example.com")
            joined = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Cut"]})
            self.assertEqual(joined.status_code, 201)
            self.assertEqual(joined.json["queue"]["predictedWaitMinutes"], 0)
            queue_id = joined.json["queue"]["id"]
            self.client.post("/logout")
            self.relogin("stats-owner@example.com")
            with self.assertLogs("smartqueue", level="INFO") as captured:
                served = self.client.patch(
                    f"/api/providers/{provider_id}/queues/{queue_id}",
                    json={"action": "serve"},
                )
                self.assertEqual(served.status_code, 200)
            messages = [record.getMessage() for record in captured.records]
            self.assertTrue(any(m.startswith("queue.prediction_outcome") for m in messages))
            completed = self.client.patch(
                f"/api/providers/{provider_id}/queues/{queue_id}",
                json={"action": "complete"},
            )
            self.assertEqual(completed.status_code, 200)
            self.client.post("/logout")

        provider = self.store.get("providers", provider_id)
        self.assertEqual(provider["serviceStats"]["Cut"]["count"], 3)
        self.assertEqual(provider["overallStats"]["count"], 3)

        # With three measured (near-instant) services, the estimate for the
        # next Cut is the measured ~1 minute, not the configured 15.
        self.relogin("stats-owner@example.com")
        walk_in = self.client.post(f"/api/providers/{provider_id}/walk-ins", json={
            "name": "Walk In", "phone": "98765 43210", "service": "Cut",
        })
        self.assertEqual(walk_in.status_code, 201, walk_in.get_data(as_text=True))
        self.relogin("stats-customer@example.com")
        joined = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Cut"]})
        self.assertEqual(joined.status_code, 201)
        self.assertEqual(joined.json["queue"]["predictedWaitMinutes"], 1)

    def test_hindi_name_transliteration_assets(self):
        script = self.client.get("/static/i18n.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn(b"transliterateName", script.data)
        self.assertIn(b"data-i18n-name", script.data)
        self.assertIn("कुमार".encode(), script.data)
        app_script = self.client.get("/static/app.js")
        self.assertEqual(app_script.status_code, 200)
        self.assertIn(b"refresh-my-queue", app_script.data)
        self.assertIn(b"refresh-dashboard", app_script.data)
        self.assertIn(b"300000", app_script.data)

    def test_multi_staff_serving_up_to_staff_count(self):
        provider_user = self.create_account("provider", "staff-owner@example.com")
        provider_id = str(provider_user["providerId"])
        queue_ids = []
        for index in range(3):
            email = f"staff-customer-{index}@example.com"
            self.fresh_csrf()
            self.create_account("customer", email)
            self.relogin(email)
            joined = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Cut"]})
            self.assertEqual(joined.status_code, 201, joined.get_data(as_text=True))
            queue_ids.append(joined.json["queue"]["id"])
            self.client.post("/logout")

        def serve(queue_id):
            return self.client.patch(
                f"/api/providers/{provider_id}/queues/{queue_id}",
                json={"action": "serve"},
            )

        self.relogin("staff-owner@example.com")
        # Default is one at a time.
        self.assertEqual(serve(queue_ids[0]).status_code, 200)
        blocked = serve(queue_ids[1])
        self.assertEqual(blocked.status_code, 409)
        self.assertIn("All your staff are busy", blocked.get_data(as_text=True))

        # Staff count validates and persists.
        self.assertEqual(
            self.client.patch(f"/api/providers/{provider_id}", json={"staffCount": 0}).status_code, 400
        )
        self.assertEqual(
            self.client.patch(f"/api/providers/{provider_id}", json={"staffCount": 21}).status_code, 400
        )
        saved = self.client.patch(f"/api/providers/{provider_id}", json={"staffCount": 2})
        self.assertEqual(saved.status_code, 200, saved.get_data(as_text=True))
        self.assertEqual(self.store.get("providers", provider_id)["staffCount"], 2)

        # Two can now be served in parallel; the third still waits.
        self.assertEqual(serve(queue_ids[1]).status_code, 200)
        self.assertEqual(serve(queue_ids[2]).status_code, 409)
        call_next = self.client.post(f"/api/providers/{provider_id}/call-next")
        self.assertEqual(call_next.status_code, 409)

        # Completing one frees a slot for the third customer.
        completed = self.client.patch(
            f"/api/providers/{provider_id}/queues/{queue_ids[0]}",
            json={"action": "complete"},
        )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(serve(queue_ids[2]).status_code, 200)

        page = self.client.get("/?view=provider")
        self.assertIn(b'name="staffCount"', page.data)
        self.assertIn(b'data-staff-count="2"', page.data)
        self.assertIn(b'id="reports-card"', page.data)
        self.assertIn(b'id="refresh-dashboard"', page.data)

    def test_multi_staff_migration_drops_one_serving_index(self):
        migration = Path("migrations/003_multi_staff_serving.sql").read_text()
        self.assertIn("DROP INDEX IF EXISTS queue_one_serving_visit_per_provider", migration)

    def test_provider_reports_periods_and_services(self):
        provider_user = self.create_account("provider", "reports-owner@example.com")
        provider_id = str(provider_user["providerId"])

        self.fresh_csrf()
        self.create_account("customer", "reports-customer-a@example.com")
        self.relogin("reports-customer-a@example.com")
        joined = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Cut"]})
        served_id = joined.json["queue"]["id"]
        self.client.post("/logout")

        self.fresh_csrf()
        self.create_account("customer", "reports-customer-b@example.com")
        self.relogin("reports-customer-b@example.com")
        joined = self.client.post("/api/queues", json={"providerId": provider_id, "services": ["Color"]})
        removed_id = joined.json["queue"]["id"]
        self.client.post("/logout")

        self.relogin("reports-owner@example.com")
        self.assertEqual(
            self.client.patch(
                f"/api/providers/{provider_id}/queues/{served_id}", json={"action": "serve"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/providers/{provider_id}/queues/{served_id}", json={"action": "complete"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/providers/{provider_id}/queues/{removed_id}",
                json={"action": "remove", "reason": "no_show"},
            ).status_code,
            200,
        )

        for period in ("day", "week", "month", "all"):
            response = self.client.get(f"/api/providers/{provider_id}/reports?period={period}")
            self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
            report = response.json["report"]
            self.assertEqual(report["period"], period)
            self.assertEqual(report["served"], 1)
            self.assertEqual(report["joined"], 2)
            self.assertEqual(report["noShows"], 1)
            self.assertEqual(report["removed"], 0)
            self.assertEqual(report["services"][0]["name"], "Cut")
            self.assertEqual(report["services"][0]["count"], 1)
            self.assertIsNotNone(report["services"][0]["avgMinutes"])
        day = self.client.get(f"/api/providers/{provider_id}/reports?period=day").json["report"]
        self.assertTrue(day["label"].startswith("Today"))
        bad = self.client.get(f"/api/providers/{provider_id}/reports?period=year")
        self.assertEqual(bad.status_code, 400)

        # Another provider cannot read these reports.
        self.client.post("/logout")
        self.fresh_csrf()
        other = self.create_account("provider", "reports-other@example.com")
        self.relogin("reports-other@example.com")
        denied = self.client.get(f"/api/providers/{provider_id}/reports?period=day")
        self.assertEqual(denied.status_code, 403)
        own = self.client.get(f"/api/providers/{other['providerId']}/reports?period=all")
        self.assertEqual(own.status_code, 200)
        self.assertEqual(own.json["report"]["served"], 0)
        self.assertEqual(own.json["report"]["services"], [])

    def test_dashboard_walk_in_accepts_multiple_services(self):
        provider_user = self.create_account("provider", "walkin-owner@example.com")
        provider_id = str(provider_user["providerId"])
        self.relogin("walkin-owner@example.com")

        page = self.client.get("/?view=provider")
        self.assertEqual(page.status_code, 200)
        # The dashboard walk-in form offers services as checkboxes, the
        # business name heads the dashboard, and it sits under the QR code.
        self.assertIn(b'type="checkbox" name="services"', page.data)
        self.assertIn(b"data-i18n-name>Jamie&#39;s Service", page.data)
        self.assertIn(b"provider-qr-name", page.data)
        # The Admin link lives at the bottom, not in the header.
        self.assertNotIn(b"nav-link admin-link", page.data)
        self.assertIn(b"dashboard-footer-links", page.data)
        landing = self.client.get("/")
        self.assertNotIn(b"nav-link admin-link", landing.data)
        self.assertIn(b">Admin</a>", landing.data)
        # Walk-in feedback appears next to the form (not only in the
        # page-top banner), and the service checkboxes are protected from
        # the settings-form full-width input styling.
        served_js = self.client.get("/static/app.js").data
        self.assertIn(b"walk-in-notice", served_js)
        self.assertIn(b"Walk-in added to the queue.", served_js)
        served_css = self.client.get("/static/styles.css").data
        self.assertIn(b".service-option input { width: auto !important", served_css)

        created = self.client.post(f"/api/providers/{provider_id}/walk-ins", json={
            "name": "Counter Guest", "phone": "98765 43210",
            "services": ["Cut", "Color"],
        })
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        entry = self.store.get("queues", created.json["queue"]["id"])
        self.assertEqual(entry["service"], "Cut, Color")

        # Picking none still books the provider's first service.
        created = self.client.post(f"/api/providers/{provider_id}/walk-ins", json={
            "name": "No Pick", "phone": "98765 43211", "services": [],
        })
        self.assertEqual(created.status_code, 201, created.get_data(as_text=True))
        entry = self.store.get("queues", created.json["queue"]["id"])
        self.assertEqual(entry["service"], "Cut")

    def test_walk_in_needs_only_email_or_phone(self):
        provider_user = self.create_account("provider", "either-owner@example.com")
        provider_id = str(provider_user["providerId"])
        self.relogin("either-owner@example.com")

        # Email alone is enough.
        email_only = self.client.post(f"/api/providers/{provider_id}/walk-ins", json={
            "name": "Email Only", "email": "guest@example.com",
        })
        self.assertEqual(email_only.status_code, 201, email_only.get_data(as_text=True))

        # Phone alone is enough.
        phone_only = self.client.post(f"/api/providers/{provider_id}/walk-ins", json={
            "name": "Phone Only", "phone": "98765 43212",
        })
        self.assertEqual(phone_only.status_code, 201, phone_only.get_data(as_text=True))

        # But one of the two is required.
        neither = self.client.post(f"/api/providers/{provider_id}/walk-ins", json={
            "name": "No Contact",
        })
        self.assertEqual(neither.status_code, 400)

        # The expanded (presentation) dashboard keeps the business name
        # visible: the stylesheet no longer hides the title block.
        served_css = self.client.get("/static/styles.css").data
        self.assertNotIn(b".presentation-expanded .dashboard-top > div:first-child", served_css)

    def test_provider_photo_upload_uses_database_storage(self):
        provider_user = self.create_account("provider", "photo-owner@example.com")
        provider_id = str(provider_user["providerId"])
        self.relogin("photo-owner@example.com")

        image_buffer = BytesIO()
        Image.new("RGB", (48, 36), color=(30, 80, 120)).save(image_buffer, format="PNG")
        image_buffer.seek(0)
        uploaded = self.client.post(
            f"/api/providers/{provider_id}/photo",
            data={"photo": (image_buffer, "shop.png", "image/png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 200, uploaded.get_data(as_text=True))
        photo_id = uploaded.json["provider"]["photoId"]
        self.assertTrue(photo_id)

        # The photo lives in the store (the database in production), not
        # only as a file on the server's disk, so it survives redeploys.
        record = self.store.get("provider_photos", photo_id.removesuffix(".jpg"))
        self.assertIsNotNone(record)
        self.assertTrue(record["data"])

        served = self.client.get(f"/media/providers/{photo_id}")
        self.assertEqual(served.status_code, 200)
        self.assertEqual(served.mimetype, "image/jpeg")
        served.get_data()
        served.close()

        # It shows beside the business name on the provider dashboard and
        # on the discover card.
        dashboard = self.client.get("/?view=provider")
        self.assertIn(b"dashboard-top has-photo", dashboard.data)
        self.assertIn(f"--provider-photo: url('/media/providers/{photo_id}')".encode(), dashboard.data)
        card = self.client.get("/")
        self.assertIn(b"provider-photo", card.data)

        # Replacing the photo removes the old one.
        image_buffer = BytesIO()
        Image.new("RGB", (48, 36), color=(120, 40, 30)).save(image_buffer, format="PNG")
        image_buffer.seek(0)
        replaced = self.client.post(
            f"/api/providers/{provider_id}/photo",
            data={"photo": (image_buffer, "shop2.png", "image/png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(replaced.status_code, 200, replaced.get_data(as_text=True))
        new_photo_id = replaced.json["provider"]["photoId"]
        self.assertNotEqual(new_photo_id, photo_id)
        self.assertIsNone(self.store.get("provider_photos", photo_id.removesuffix(".jpg")))
        self.assertEqual(self.client.get(f"/media/providers/{photo_id}").status_code, 404)
        self.assertEqual(self.client.get(f"/media/providers/{new_photo_id}").status_code, 200)

        # Migration 004 creates the backing table.
        migration = Path("migrations/004_provider_photos.sql").read_text()
        self.assertIn("provider_photos", migration)



if __name__ == "__main__":
    unittest.main()
