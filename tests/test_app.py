from io import BytesIO
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from uuid import uuid4

from PIL import Image

from app import create_app
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


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "DATA_DIR": Path(self.temp_dir.name) / "store",
            "SEED_DIR": ROOT / "data" / "seed" / "providers",
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
        return next(user for user in self.store.list("users") if user.get("email") == data["email"] or user.get("phone") == data["phone"])

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

    def test_seed_and_home_render(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Willow &amp; Sage", response.data)
        self.assertIn(b"Sign up", response.data)
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
        self.assertEqual(admin.status_code, 200)
        self.assertIn(b"Platform overview", admin.data)
        self.assertEqual(script.status_code, 200)
        self.assertEqual(styles.status_code, 200)
        script.close()
        styles.close()

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
        self.assertIn(provider, self.client.get("/api/providers").json)

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
        self.assertEqual(current.json["id"], queue["id"])
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

    def test_provider_status_controls_customer_join_and_switching_queues(self):
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
        self.assertIsNone(self.store.get("queues", second_queue_id))
        self.assertEqual(self.store.get("providers", str(second_provider["providerId"]))["people"], 0)
        self.assertEqual(self.store.get("providers", provider_id)["people"], 1)
        self.assertEqual(self.client.get("/api/queues/current").json["providerId"], first_provider["providerId"])

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
        self.assertEqual(current.json["position"], 1)
        self.assertEqual(current.json["wait"], 0)
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
        photo_path = Path(self.app.config["DATA_DIR"]) / "provider_photos" / photo_id
        self.assertTrue(photo_path.is_file())
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
        self.assertEqual(current.json["customerId"], second_customer["id"])
        self.assertEqual(current.json["position"], 1)
        self.assertEqual(current.json["wait"], 0)

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
        self.assertIn(b"Log in to SmartQueue", register.data)
        self.assertNotIn(b"will be added separately", register.data)



if __name__ == "__main__":
    unittest.main()
