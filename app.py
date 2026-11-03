import io
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from email.utils import parseaddr
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash

from store import FileStore


ROOT = Path(__file__).resolve().parent
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
PHONE_INPUT = re.compile(r"^[+\d().\-\s]+$")
PHONE_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
SUPPORTED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}
MAX_IMAGE_PIXELS = 20_000_000
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
REGISTRATION_DRAFT_TTL = 60 * 60
PROVIDER_CATEGORIES = {
    "Salon": ("lavender", "✂"),
    "Healthcare": ("mint", "✚"),
    "Food & drink": ("peach", "☕"),
    "Government": ("blue", "⌂"),
    "Other": ("mint", "✳"),
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def create_app(test_config=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_mapping(
        DATA_DIR=ROOT / "data" / "store",
        SEED_DIR=ROOT / "data" / "seed" / "providers",
        MAX_CONTENT_LENGTH=MAX_UPLOAD_BYTES + 64 * 1024,
        SECRET_KEY=os.environ.get("SMARTQUEUE_SECRET_KEY") or secrets.token_hex(32),
    )
    if test_config:
        app.config.update(test_config)
    store = FileStore(app.config["DATA_DIR"])
    app.extensions["smartqueue_store"] = store

    def seed_providers():
        if store.list("providers"):
            return
        for seed_file in sorted(Path(app.config["SEED_DIR"]).glob("*.json")):
            with seed_file.open(encoding="utf-8") as file:
                provider = json.load(file)
            if not isinstance(provider.get("id"), int):
                raise ValueError(f"Invalid provider seed: {seed_file.name}")
            store.put("providers", str(provider["id"]), provider, provider)

    def require_provider(provider_id):
        if not isinstance(provider_id, str) or not CLIENT_ID.fullmatch(provider_id):
            raise ApiError(400, "A valid provider ID is required.")
        provider = store.get("providers", provider_id)
        if provider is None:
            raise ApiError(404, "Provider not found.")
        return provider

    def current_user():
        user_id = session.get("user_id")
        return store.get("users", user_id) if isinstance(user_id, str) else None

    def require_user(role=None):
        user = current_user()
        if not user:
            raise ApiError(401, "Please sign in to continue.")
        if role and user.get("role") != role:
            raise ApiError(403, f"A {role} account is required.")
        return user

    def require_owned_provider(provider_id):
        user = require_user("provider")
        provider = require_provider(provider_id)
        if str(provider.get("ownerId")) != user["id"]:
            raise ApiError(403, "You can only manage your own provider listing.")
        return user, provider

    def active_provider_queues(provider_id):
        return sorted(
            (
                entry for entry in store.list("queues")
                if str(entry.get("providerId")) == str(provider_id)
                and entry.get("status") in {"waiting", "serving"}
            ),
            key=lambda entry: (entry.get("status") != "serving", entry.get("createdAt", "")),
        )

    def refresh_provider_metrics(provider):
        active = active_provider_queues(provider["id"])
        provider["people"] = len(active)
        provider["wait"] = len(active) * max(1, int(provider.get("serviceDuration", 15)))
        return provider

    def parse_services(value):
        services = [item.strip() for item in value.split(",") if item.strip()]
        if len(services) > 20 or any(len(service) > 60 for service in services):
            return None
        return services

    def csrf_token():
        token = session.get("csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["csrf_token"] = token
        return token

    def registration_photo_path():
        draft_id = session.get("registration_photo_draft")
        if not isinstance(draft_id, str) or not re.fullmatch(r"[a-f0-9]{32}\.jpg", draft_id):
            session.pop("registration_photo_draft", None)
            return None
        path = Path(app.config["DATA_DIR"]) / "registration_drafts" / draft_id
        if not path.is_file():
            session.pop("registration_photo_draft", None)
            return None
        return path

    def cleanup_expired_registration_photos():
        draft_dir = Path(app.config["DATA_DIR"]) / "registration_drafts"
        if not draft_dir.exists():
            return
        cutoff = time.time() - REGISTRATION_DRAFT_TTL
        for path in draft_dir.glob("*.jpg"):
            if path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

    def clear_registration_photo_draft():
        path = registration_photo_path()
        session.pop("registration_photo_draft", None)
        if path:
            path.unlink(missing_ok=True)

    @app.context_processor
    def inject_template_state():
        return {"current_user": current_user(), "csrf_token": csrf_token}

    def render_registration(form=None, errors=None, created=False, status=200, photo_staged=False):
        return (
            render_template(
                "register.html",
                form=form or {},
                errors=errors or {},
                provider_categories=PROVIDER_CATEGORIES,
                created=created,
                photo_staged=photo_staged,
            ),
            status,
        )

    def process_photo(upload):
        if not upload or not upload.filename:
            return None, None
        contents = upload.stream.read(MAX_UPLOAD_BYTES + 1)
        if len(contents) > MAX_UPLOAD_BYTES:
            return None, "Choose an image smaller than 5 MB."
        try:
            with Image.open(io.BytesIO(contents)) as image:
                if image.format not in SUPPORTED_IMAGE_FORMATS:
                    return None, "Upload a JPEG, PNG, or WebP image."
                if image.width * image.height > MAX_IMAGE_PIXELS:
                    return None, "Choose an image with dimensions under 20 megapixels."
                image.verify()
            with Image.open(io.BytesIO(contents)) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((1600, 1600), Image.Resampling.LANCZOS)
                if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                    foreground = image.convert("RGBA")
                    background = Image.new("RGB", foreground.size, "white")
                    background.paste(foreground, mask=foreground.getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")
                output = io.BytesIO()
                image.save(output, format="JPEG", quality=86, optimize=True)
                return output.getvalue(), None
        except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
            return None, "The selected file is not a valid supported image."

    @app.get("/media/providers/<photo_id>")
    def provider_photo(photo_id):
        if not re.fullmatch(r"[a-f0-9]{32}\.jpg", photo_id):
            abort(404)
        photo_path = Path(app.config["DATA_DIR"]) / "provider_photos" / photo_id
        if not photo_path.is_file():
            abort(404)
        response = send_file(photo_path, mimetype="image/jpeg", conditional=True)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    @app.before_request
    def initialize_data():
        seed_providers()

    @app.before_request
    def protect_mutations():
        if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
            return None
        expected = session.get("csrf_token")
        supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not expected or not supplied or not secrets.compare_digest(expected, supplied):
            if request.path.startswith("/api/"):
                return jsonify(error="The request token is missing or invalid. Refresh and retry."), 400
            if request.endpoint == "login":
                return render_template("login.html", error="The page expired. Refresh and try again."), 400
            if request.endpoint == "register":
                return render_registration(
                    errors={"form": "The page expired. Refresh and try again."},
                    status=400,
                    photo_staged=registration_photo_path() is not None,
                )
            abort(400, description="The request token is missing or invalid. Refresh and try again.")
        return None

    @app.errorhandler(ApiError)
    def handle_api_error(error):
        return jsonify(error=str(error)), error.status

    @app.errorhandler(413)
    def request_too_large(_error):
        if request.endpoint == "register":
            return render_registration(
                {},
                {"photo": "The registration upload is too large; images must be under 5 MB."},
                status=413,
                photo_staged=registration_photo_path() is not None,
            )
        return jsonify(error="Request body is too large."), 413

    @app.get("/")
    def home():
        providers = store.list("providers")
        queue = None
        user = current_user()
        if user and user.get("role") == "customer":
            queue = next(
                (
                    entry
                    for entry in store.list("queues")
                    if entry.get("customerId") == user["id"]
                    and entry.get("status") in {"waiting", "serving"}
                ),
                None,
            )
        view = request.args.get("view", "customer")
        customer_position = None
        customer_wait = 0
        if queue:
            active = active_provider_queues(queue["providerId"])
            position = next((index for index, entry in enumerate(active) if entry["id"] == queue["id"]), None)
            provider_for_queue = store.get("providers", str(queue["providerId"]))
            duration = max(1, int(provider_for_queue.get("serviceDuration", 15)))
            customer_position = "Now" if queue.get("status") == "serving" else ((position + 1) if position is not None else "—")
            customer_wait = duration * max(0, position or 0)
            queue = {**queue, "wait": customer_wait}
        if view not in {"customer", "provider", "admin"}:
            view = "customer"
        provider = None
        provider_queue = []
        if view == "provider":
            user = current_user()
            if not user:
                return redirect(url_for("login"))
            if user.get("role") != "provider":
                return redirect(url_for("home"))
            provider_id = user.get("providerId")
            provider = store.get("providers", str(provider_id)) if provider_id else None
            if not provider or str(provider.get("ownerId")) != user["id"]:
                session.clear()
                return redirect(url_for("login"))
            provider_queue = active_provider_queues(provider["id"])
            service_duration = max(1, int(provider.get("serviceDuration", 15)))
            service_in_progress = any(entry.get("status") == "serving" for entry in provider_queue)
            waiting_ahead = 0
            for entry in provider_queue:
                customer_id = entry.get("customerId")
                customer = store.get("users", customer_id) if isinstance(customer_id, str) else None
                entry["customerName"] = customer.get("name", "Customer") if customer else "Customer"
                if entry["status"] == "serving":
                    entry["estimatedWait"] = 0
                else:
                    entry["estimatedWait"] = (
                        service_duration * (waiting_ahead + sum(item.get("status") == "serving" for item in provider_queue))
                    )
                    waiting_ahead += 1
            provider = refresh_provider_metrics(provider)
        return render_template(
            "index.html",
            providers=providers,
            queue=queue,
            view=view,
            provider=provider,
            provider_queue=provider_queue,
            service_in_progress=service_in_progress if view == "provider" else False,
            provider_categories=PROVIDER_CATEGORIES,
            customer_position=customer_position,
            customer_wait=customer_wait,
            favorite_ids={str(item) for item in user.get("favoriteProviderIds", [])} if user and user.get("role") == "customer" else set(),
        )

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return render_template("login.html", error=None)
        identifier = request.form.get("identifier", "").strip()
        email_identifier = identifier.lower()
        phone_identifier = ""
        if identifier.startswith("+") and PHONE_INPUT.fullmatch(identifier):
            phone_identifier = "+" + re.sub(r"\D", "", identifier)
        user = next(
            (item for item in store.list("users")
             if (email_identifier and (item.get("email") or "").casefold() == email_identifier)
             or (phone_identifier and item.get("phone") == phone_identifier)),
            None,
        )
        record = store.get("users", user["id"]) if user else None
        if not record or not check_password_hash(record.get("passwordHash", ""), request.form.get("password", "")):
            return render_template("login.html", error="Email/phone or password is incorrect."), 401
        session.clear()
        session["user_id"] = record["id"]
        session["csrf_token"] = secrets.token_urlsafe(32)
        destination = url_for("home", view="provider") if record["role"] == "provider" else url_for("home")
        return redirect(destination, code=303)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"), code=303)

    @app.route("/register", methods=["GET", "POST"])
    def register():
        cleanup_expired_registration_photos()
        if request.method == "GET":
            return render_registration(
                created=request.args.get("created") == "1",
                photo_staged=registration_photo_path() is not None,
            )

        form = {
            "name": request.form.get("name", "").strip(),
            "email": request.form.get("email", "").strip().lower(),
            "phone": request.form.get("phone", "").strip(),
            "role": request.form.get("role", "customer").strip().lower(),
            "business_name": request.form.get("business_name", "").strip(),
            "category": request.form.get("category", "").strip(),
            "address": request.form.get("address", "").strip(),
            "services": request.form.get("services", "").strip(),
        }
        password = request.form.get("password", "")
        errors = {}
        normalized_phone = ""
        if form["email"]:
            _, parsed_email = parseaddr(form["email"])
            if (
                len(form["email"]) > 254
                or parsed_email != form["email"]
                or "@" not in form["email"]
                or "." not in form["email"].rsplit("@", 1)[-1]
            ):
                errors["email"] = "Enter a valid email address."
        if form["phone"]:
            phone_input = form["phone"].strip()
            if (
                not PHONE_INPUT.fullmatch(phone_input)
                or not phone_input.startswith("+")
                or phone_input.count("+") != 1
            ):
                errors["phone"] = "Enter a valid international phone number, including the + country code."
            else:
                normalized_phone = "+" + re.sub(r"\D", "", phone_input)
                if not PHONE_E164.fullmatch(normalized_phone):
                    errors["phone"] = "Enter a valid international phone number, including the + country code."
        if not form["email"] and not form["phone"]:
            errors["contact"] = "Enter an email address, a phone number, or both."
        if not form["name"] or len(form["name"]) > 100:
            errors["name"] = "Enter your name (up to 100 characters)."
        if len(password) < 10 or len(password) > 128:
            errors["password"] = "Use a password between 10 and 128 characters."
        if form["role"] not in {"customer", "provider"}:
            errors["role"] = "Choose a customer or provider account."
        services = parse_services(form["services"])
        if form["role"] == "provider":
            if not form["business_name"] or len(form["business_name"]) > 120:
                errors["business_name"] = "Enter your business name (up to 120 characters)."
            if form["category"] not in PROVIDER_CATEGORIES:
                errors["category"] = "Choose a business category."
            if not form["address"] or len(form["address"]) > 200:
                errors["address"] = "Enter your business address (up to 200 characters)."
            if services is None:
                errors["services"] = "Enter up to 20 services, each no longer than 60 characters."
        photo_contents, photo_error = process_photo(request.files.get("photo"))
        if photo_error:
            errors["photo"] = photo_error
        staged_photo_path = registration_photo_path()
        if photo_contents:
            draft_id = f"{uuid.uuid4().hex}.jpg"
            staged_photo_path = Path(app.config["DATA_DIR"]) / "registration_drafts" / draft_id
            staged_photo_path.parent.mkdir(parents=True, exist_ok=True)
            staged_photo_path.write_bytes(photo_contents)
            previous_photo_path = registration_photo_path()
            session["registration_photo_draft"] = draft_id
            if previous_photo_path:
                previous_photo_path.unlink(missing_ok=True)
        elif staged_photo_path:
            photo_contents = staged_photo_path.read_bytes()
        if errors:
            return render_registration(form, errors, status=400, photo_staged=staged_photo_path is not None)

        with store.transaction():
            users = store.list("users")
            if form["email"] and any(
                user.get("email") and user["email"].casefold() == form["email"].casefold()
                for user in users
            ):
                errors["email"] = "An account with this email already exists."
            if normalized_phone and any(user.get("phone") == normalized_phone for user in users):
                errors["phone"] = "An account with this phone number already exists."
            if errors:
                return render_registration(form, errors, status=409, photo_staged=staged_photo_path is not None)

            user_id = str(uuid.uuid4())
            user = {
                "id": user_id,
                "name": form["name"],
                "email": form["email"] or None,
                "phone": normalized_phone or None,
                "role": form["role"],
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            provider = None
            photo_id = f"{uuid.uuid4().hex}.jpg" if photo_contents else None

            if form["role"] == "provider":
                providers = store.list("providers")
                provider_id = str(max((int(item["id"]) for item in providers), default=0) + 1)
                accent, icon = PROVIDER_CATEGORIES[form["category"]]
                provider = {
                    "id": int(provider_id),
                    "ownerId": user_id,
                    "name": form["business_name"],
                    "category": form["category"],
                    "description": "",
                    "address": form["address"],
                    "distance": "New listing",
                    "wait": 0,
                    "people": 0,
                    "serviceDuration": 15,
                    "rating": "New",
                    "reviews": 0,
                    "status": "Open",
                    "hours": "Hours not set",
                    "accent": accent,
                    "icon": icon,
                    "services": services,
                }
                if photo_id:
                    provider["photoId"] = photo_id
                user["providerId"] = provider["id"]

            if photo_id:
                user["photoId"] = photo_id
            user["passwordHash"] = generate_password_hash(password)
            index_entry = {key: value for key, value in user.items() if key != "passwordHash"}
            photo_path = (
                Path(app.config["DATA_DIR"])
                / ("provider_photos" if form["role"] == "provider" else "customer_photos")
                / photo_id
                if photo_id
                else None
            )
            photo_written = False
            try:
                if photo_path:
                    photo_path.parent.mkdir(parents=True, exist_ok=True)
                    photo_written = True
                    photo_path.write_bytes(photo_contents)
                user_created = store.create("users", user_id, user, index_entry)
            except OSError:
                if photo_written:
                    photo_path.unlink(missing_ok=True)
                store.delete("users", user_id)
                raise
            if not user_created:
                if photo_written:
                    photo_path.unlink(missing_ok=True)
                raise RuntimeError("Could not create the account record.")
            if provider is not None:
                provider_id = str(provider["id"])
                try:
                    provider_created = store.create("providers", provider_id, provider, provider)
                except OSError:
                    store.delete("providers", provider_id)
                    store.delete("users", user_id)
                    if photo_written:
                        photo_path.unlink(missing_ok=True)
                    raise
                if not provider_created:
                    store.delete("users", user_id)
                    if photo_written:
                        photo_path.unlink(missing_ok=True)
                    raise RuntimeError("Could not create the provider listing.")

        clear_registration_photo_draft()
        return redirect(url_for("register", created="1"), code=303)

    @app.get("/api/providers")
    def list_providers():
        return jsonify(store.list("providers"))

    @app.get("/api/queues/current")
    def current_queue():
        user = require_user("customer")
        queue = next(
            (
                entry
                for entry in store.list("queues")
                if entry.get("customerId") == user["id"]
                and entry.get("status") in {"waiting", "serving"}
            ),
            None,
        )
        if queue:
            active = active_provider_queues(queue["providerId"])
            position = next((index for index, entry in enumerate(active) if entry["id"] == queue["id"]), None)
            provider = require_provider(str(queue["providerId"]))
            duration = max(1, int(provider.get("serviceDuration", 15)))
            queue = {
                **queue,
                "peopleAhead": max(0, (position or 0) - (1 if queue.get("status") == "serving" else 0)),
                "wait": duration * max(0, position or 0),
                "position": "Now" if queue.get("status") == "serving" else (position + 1 if position is not None else None),
            }
        return jsonify(queue)

    @app.post("/api/queues")
    def join_queue():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        user = require_user("customer")
        provider_id = body.get("providerId")
        if not isinstance(provider_id, (str, int)) or not CLIENT_ID.fullmatch(str(provider_id)):
            raise ApiError(400, "A valid provider ID is required.")
        provider_id = str(provider_id)

        with store.transaction():
            provider = require_provider(provider_id)
            if provider.get("status") != "Open":
                raise ApiError(409, "This provider is not accepting customers right now.")
            queues = store.list("queues")
            existing = next(
                (
                    entry
                    for entry in queues
                    if entry.get("customerId") == user["id"]
                    and entry.get("status") in {"waiting", "serving"}
                ),
                None,
            )
            if existing and str(existing["providerId"]) == provider_id:
                return jsonify(queue=existing, providers=store.list("providers")), 200
            if existing:
                if existing.get("status") == "serving":
                    raise ApiError(409, "You cannot leave while a provider is serving you.")
                previous = require_provider(str(existing["providerId"]))
                store.delete("queues", existing["id"])
                refresh_provider_metrics(previous)
                store.put("providers", str(previous["id"]), previous, previous)

            active_queues = active_provider_queues(provider_id)
            service_duration = max(1, int(provider.get("serviceDuration", 15)))
            queue = {
                "id": str(uuid.uuid4()),
                "customerId": user["id"],
                "providerId": provider["id"],
                "peopleAhead": len(active_queues),
                "wait": len(active_queues) * service_duration,
                "status": "waiting",
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            store.create("queues", queue["id"], queue, queue)
            refresh_provider_metrics(provider)
            store.put("providers", str(provider["id"]), provider, provider)
            return jsonify(queue=queue, providers=store.list("providers")), 201

    @app.delete("/api/queues/<queue_id>")
    def leave_queue(queue_id):
        user = require_user("customer")
        if not CLIENT_ID.fullmatch(queue_id):
            raise ApiError(400, "A valid queue ID is required.")
        with store.transaction():
            queue = store.get("queues", queue_id)
            if (
                not queue
                or queue.get("customerId") != user["id"]
                or queue.get("status") != "waiting"
            ):
                raise ApiError(404, "Active queue entry not found.")
            provider = require_provider(str(queue["providerId"]))
            store.delete("queues", queue_id)
            refresh_provider_metrics(provider)
            store.put("providers", str(provider["id"]), provider, provider)
            return jsonify(providers=store.list("providers"))

    @app.post("/api/providers/<provider_id>/call-next")
    def call_next(provider_id):
        with store.transaction():
            _, provider = require_owned_provider(provider_id)
            if any(queue.get("status") == "serving" for queue in active_provider_queues(provider_id)):
                raise ApiError(409, "Complete the current service before starting another.")
            waiting = [entry for entry in active_provider_queues(provider_id) if entry.get("status") == "waiting"]
            if not waiting:
                raise ApiError(409, "There are no waiting customers.")
            entry = waiting[0]
            entry["status"] = "serving"
            entry["startedAt"] = datetime.now(timezone.utc).isoformat()
            store.put("queues", entry["id"], entry, entry)
            refresh_provider_metrics(provider)
            store.put("providers", str(provider["id"]), provider, provider)
            return jsonify(queue=entry, providers=store.list("providers"))

    @app.patch("/api/providers/<provider_id>")
    def update_provider(provider_id):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        allowed = {"status", "name", "category", "address", "services", "hours", "serviceDuration"}
        if not body or set(body) - allowed:
            raise ApiError(400, "Only provider profile and queue settings can be changed.")
        with store.transaction():
            _, provider = require_owned_provider(provider_id)
            if "status" in body:
                if not isinstance(body["status"], str) or body["status"] not in {"Open", "Paused"}:
                    raise ApiError(400, "Status must be Open or Paused.")
                provider["status"] = body["status"]
            for key, limit in (("name", 120), ("address", 200), ("hours", 100)):
                if key in body:
                    if not isinstance(body[key], str) or not body[key].strip() or len(body[key].strip()) > limit:
                        raise ApiError(400, f"{key} must contain 1 to {limit} characters.")
                    provider[key] = body[key].strip()
            if "category" in body:
                if not isinstance(body["category"], str) or body["category"] not in PROVIDER_CATEGORIES:
                    raise ApiError(400, "Choose a valid business category.")
                provider["category"] = body["category"]
                provider["accent"], provider["icon"] = PROVIDER_CATEGORIES[body["category"]]
            if "services" in body:
                services = body["services"]
                if (
                    not isinstance(services, list)
                    or len(services) > 20
                    or any(not isinstance(service, str) for service in services)
                    or any(not service.strip() or len(service.strip()) > 60 for service in services)
                ):
                    raise ApiError(400, "Services must be a list of up to 20 names.")
                provider["services"] = [service.strip() for service in services]
            if "serviceDuration" in body:
                duration = body["serviceDuration"]
                if not isinstance(duration, int) or isinstance(duration, bool) or not 1 <= duration <= 180:
                    raise ApiError(400, "Average service time must be between 1 and 180 minutes.")
                provider["serviceDuration"] = duration
                refresh_provider_metrics(provider)
            store.put("providers", str(provider["id"]), provider, provider)
            return jsonify(providers=store.list("providers"))

    @app.patch("/api/providers/<provider_id>/queues/<queue_id>")
    def update_provider_queue(provider_id, queue_id):
        body = request.get_json(silent=True)
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("action"), str)
            or body["action"] not in {"serve", "complete"}
        ):
            raise ApiError(400, "Choose serve or complete.")
        if not CLIENT_ID.fullmatch(queue_id):
            raise ApiError(400, "A valid queue ID is required.")
        with store.transaction():
            _, provider = require_owned_provider(provider_id)
            queue = store.get("queues", queue_id)
            if not queue or str(queue.get("providerId")) != provider_id:
                raise ApiError(404, "Queue entry not found.")
            if body["action"] == "serve":
                if queue.get("status") != "waiting":
                    raise ApiError(409, "Only waiting customers can be started.")
                if any(entry.get("status") == "serving" for entry in active_provider_queues(provider_id)):
                    raise ApiError(409, "Complete the current service before starting another.")
                queue["status"] = "serving"
                queue["startedAt"] = datetime.now(timezone.utc).isoformat()
            else:
                if queue.get("status") != "serving":
                    raise ApiError(409, "Only a customer currently being served can be completed.")
                queue["status"] = "completed"
                queue["completedAt"] = datetime.now(timezone.utc).isoformat()
            store.put("queues", queue["id"], queue, queue)
            refresh_provider_metrics(provider)
            store.put("providers", str(provider["id"]), provider, provider)
            return jsonify(queue=queue, providers=store.list("providers"))

    @app.post("/api/favorites/<provider_id>")
    def add_favorite(provider_id):
        user = require_user("customer")
        with store.transaction():
            provider = require_provider(provider_id)
            favorites = [str(item) for item in user.get("favoriteProviderIds", [])]
            if provider_id not in favorites:
                favorites.append(provider_id)
            user["favoriteProviderIds"] = favorites
            index_entry = {key: value for key, value in user.items() if key != "passwordHash"}
            store.put("users", user["id"], user, index_entry)
        return jsonify(favoriteProviderIds=favorites)

    @app.delete("/api/favorites/<provider_id>")
    def remove_favorite(provider_id):
        user = require_user("customer")
        with store.transaction():
            require_provider(provider_id)
            favorites = [str(item) for item in user.get("favoriteProviderIds", []) if str(item) != provider_id]
            user["favoriteProviderIds"] = favorites
            index_entry = {key: value for key, value in user.items() if key != "passwordHash"}
            store.put("users", user["id"], user, index_entry)
        return jsonify(favoriteProviderIds=favorites)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=5000)
