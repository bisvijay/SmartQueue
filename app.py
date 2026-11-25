import base64
import io
import json
import logging
import os
import re
import secrets
import time
import uuid
from io import BytesIO
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - zoneinfo is stdlib on Python 3.9+
    ZoneInfo = None

import qrcode
from flask import Flask, abort, g, jsonify, make_response, redirect, render_template, request, send_file, session, url_for
from qrcode.image.svg import SvgPathImage
from PIL import Image, ImageOps, UnidentifiedImageError
from werkzeug.security import check_password_hash, generate_password_hash

from logging_config import configure_logging
from postgres_store import PostgresStore
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
PROVIDER_TIMEZONES = {
    "Asia/Kolkata": "India (UTC+5:30)",
    "America/Los_Angeles": "US Pacific",
    "America/New_York": "US Eastern",
    "Europe/London": "London",
    "UTC": "UTC",
}
DEFAULT_PROVIDER_TIMEZONE = "Asia/Kolkata"
# Measured per-service averages only take over from the configured times once
# enough real services have been recorded for them to be meaningful.
MIN_STAT_SAMPLES = 3
TIME_INPUT = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
AD_PLACEMENTS = {"home", "dashboard"}
AD_CONTENT_TYPES = {"image", "video", "text"}
AD_EVENT_TYPES = {"started", "completed", "failed"}
MAX_AD_VIDEO_BYTES = 10 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def create_app(test_config=None):
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.from_mapping(
        DATA_DIR=ROOT / "data" / "store",
        SEED_DIR=ROOT / "data" / "seed" / "providers",
        MAX_CONTENT_LENGTH=MAX_AD_VIDEO_BYTES + 1024 * 1024,
        SECRET_KEY=os.environ.get("SMARTQUEUE_SECRET_KEY") or secrets.token_hex(32),
        ADMIN_PASSWORD=os.environ.get("SMARTQUEUE_ADMIN_PASSWORD", ""),
        DATABASE_URL=os.environ.get("SMARTQUEUE_DATABASE_URL", ""),
        DATABASE_SSL_ROOT_CERT=os.environ.get("SMARTQUEUE_DB_SSL_ROOT_CERT", ""),
        LOG_FILE=os.environ.get("SMARTQUEUE_LOG_FILE", ""),
    )
    if test_config:
        app.config.update(test_config)
    store = (
        PostgresStore(app.config["DATABASE_URL"], app.config["DATABASE_SSL_ROOT_CERT"])
        if app.config["DATABASE_URL"]
        else FileStore(app.config["DATA_DIR"])
    )
    app.config["POSTGRES_ENABLED"] = isinstance(store, PostgresStore)
    app.extensions["smartqueue_store"] = store
    log = configure_logging(app.config["DATA_DIR"], app.config.get("LOG_FILE") or None)
    log.info(
        "SmartQueue app created: store=%s",
        "postgres" if app.config["POSTGRES_ENABLED"] else "json-file",
    )

    @app.before_request
    def _log_request_start():
        g.request_started = time.perf_counter()

    @app.after_request
    def _log_request_end(response):
        started = getattr(g, "request_started", None)
        if started is not None:
            log.debug(
                "request method=%s path=%s status=%s duration_ms=%.1f",
                request.method,
                request.path,
                response.status_code,
                (time.perf_counter() - started) * 1000,
            )
        return response

    def seed_providers():
        if app.config["POSTGRES_ENABLED"]:
            return
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

    def require_admin():
        if not app.config["ADMIN_PASSWORD"] or session.get("admin_authenticated") is not True:
            raise ApiError(403, "Administrator access is required.")

    def provider_approval_status(provider):
        # Existing listings predate moderation and remain visible; newly registered
        # providers are explicitly marked pending.
        return provider.get("approvalStatus", "approved")

    def ensure_provider_approved(provider):
        if provider_approval_status(provider) != "approved":
            raise ApiError(403, "This provider is not approved to accept queue entries.")

    def provider_is_private(provider):
        # A private listing stays out of public discovery, but the provider
        # keeps a working dashboard, QR code, and walk-in queue.
        return bool(provider.get("privateListing"))

    def all_customer_reviews():
        # Until migration 006 is applied the Postgres table may not exist;
        # treat that as "no reviews yet" instead of breaking pages.
        try:
            return store.list("customer_reviews")
        except Exception:
            log.warning("customer_reviews unavailable; returning none")
            return []

    def customer_reviews_for(provider_id):
        return [
            review
            for review in all_customer_reviews()
            if str(review.get("providerId")) == str(provider_id)
        ]

    def refresh_provider_rating(provider):
        # The listing's rating and review count always reflect the real
        # customer reviews on file; with none, the listing shows as New.
        reviews = customer_reviews_for(provider["id"])
        if reviews:
            provider["reviews"] = len(reviews)
            provider["rating"] = round(
                sum(int(review["rating"]) for review in reviews) / len(reviews), 1
            )
        else:
            provider["reviews"] = 0
            provider["rating"] = "New"

    def public_customer_review(review):
        return {
            "id": review["id"],
            "providerId": str(review["providerId"]),
            "customerName": review.get("customerName") or "Customer",
            "rating": int(review["rating"]),
            "comment": review.get("comment") or "",
            "createdAt": review.get("createdAt"),
        }

    def require_owned_provider(provider_id):
        user = require_user("provider")
        provider = require_provider(provider_id)
        if str(provider.get("ownerId")) != user["id"]:
            raise ApiError(403, "You can only manage your own provider listing.")
        return user, provider

    def provider_with_contact(provider):
        provider = dict(provider)
        owner_id = provider.get("ownerId")
        owner = store.get("users", owner_id) if isinstance(owner_id, str) else None
        provider["contactEmail"] = provider.get("contactEmail") or (owner.get("email") if owner else None)
        provider["contactPhone"] = provider.get("contactPhone") or (owner.get("phone") if owner else None)
        return provider

    def active_provider_queues(provider_id):
        # Serving first, then regular waiting in join order, then the buffer
        # waitlist in join order.
        return sorted(
            (
                entry for entry in store.list("queues")
                if str(entry.get("providerId")) == str(provider_id)
                and entry.get("status") in {"waiting", "serving"}
            ),
            key=lambda entry: (
                entry.get("status") != "serving",
                bool(entry.get("buffer")),
                entry.get("createdAt", ""),
            ),
        )

    def provider_service_minutes(provider):
        """Configured minutes per service name; anything unusable is ignored."""
        configured = provider.get("serviceDurations")
        if not isinstance(configured, dict):
            return {}
        minutes = {}
        for name, value in configured.items():
            if isinstance(value, bool):
                continue
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if isinstance(name, str) and name.strip() and 1 <= value <= 180:
                minutes[name.strip()] = value
        return minutes

    def service_names(service_field):
        return [part.strip() for part in str(service_field or "").split(",") if part.strip()]

    def cleaned_stats(raw):
        """Normalize a {avg, count} measurement record; anything unusable -> None."""
        if not isinstance(raw, dict):
            return None
        try:
            avg = float(raw.get("avg"))
            count = int(raw.get("count"))
        except (TypeError, ValueError):
            return None
        if avg <= 0 or count <= 0:
            return None
        return {"avg": avg, "count": count}

    def service_measured_minutes(provider, name):
        """Measured average minutes for one service, once enough data exists."""
        stats = provider.get("serviceStats")
        if isinstance(stats, dict):
            measured = cleaned_stats(stats.get(name))
            if measured and measured["count"] >= MIN_STAT_SAMPLES:
                return max(1, round(measured["avg"]))
        return None

    def effective_service_minutes(provider, name):
        """Best estimate for one service: measured average, then the
        provider's configured time for it, then the measured overall
        average, then the provider's configured average."""
        measured = service_measured_minutes(provider, name)
        if measured is not None:
            return measured
        configured = provider_service_minutes(provider)
        if name in configured:
            return configured[name]
        overall = cleaned_stats(provider.get("overallStats"))
        if overall and overall["count"] >= MIN_STAT_SAMPLES:
            return max(1, round(overall["avg"]))
        return max(1, int(provider.get("serviceDuration", 15)))

    def visit_minutes(provider, service_field):
        """Minutes one booked visit takes: the sum of its services' times."""
        names = service_names(service_field)
        if not names:
            return max(1, int(provider.get("serviceDuration", 15)))
        return sum(effective_service_minutes(provider, name) for name in names)

    def provider_staff_limit(provider):
        """How many customers can be served at the same time: one per staff
        member, chair, or counter. Defaults to 1."""
        try:
            return max(1, min(20, int(provider.get("staffCount") or 1)))
        except (TypeError, ValueError):
            return 1

    def total_wait_minutes(provider, entries):
        total = sum(visit_minutes(provider, entry.get("service")) for entry in entries)
        # Staff serve in parallel, so the wall-clock wait is the total work
        # divided across them.
        return -(-total // provider_staff_limit(provider))

    def provider_timezone(provider):
        name = provider.get("timezone")
        if isinstance(name, str) and name in PROVIDER_TIMEZONES and ZoneInfo is not None:
            try:
                return ZoneInfo(name)
            except Exception:
                pass
        # Fallback if tzdata is unavailable: India time, the primary market.
        return timezone(timedelta(hours=5, minutes=30))

    def provider_local_now(provider, moment=None):
        moment = moment or datetime.now(timezone.utc)
        return moment.astimezone(provider_timezone(provider))

    def parse_hhmm(value):
        if not isinstance(value, str) or not TIME_INPUT.fullmatch(value.strip()):
            return None
        hours, minutes = value.strip().split(":")
        return int(hours), int(minutes)

    def provider_day_window(provider, local_now=None):
        """Today's (open, close) datetimes in the provider's timezone, or
        None when the provider has not set both opening and closing times."""
        opening = parse_hhmm(provider.get("openTime"))
        closing = parse_hhmm(provider.get("closeTime"))
        if opening is None or closing is None or closing <= opening:
            return None
        local_now = local_now or provider_local_now(provider)
        open_dt = local_now.replace(hour=opening[0], minute=opening[1], second=0, microsecond=0)
        close_dt = local_now.replace(hour=closing[0], minute=closing[1], second=0, microsecond=0)
        return open_dt, close_dt

    def format_local_time(provider, moment_utc):
        return provider_local_now(provider, moment_utc).strftime("%I:%M %p").lstrip("0")

    def format_hhmm(pair):
        hour, minute = pair
        suffix = "AM" if hour < 12 else "PM"
        return f"{hour % 12 or 12}:{minute:02d} {suffix}"

    def capacity_limit_minutes(provider, local_now=None):
        """Minutes of service the provider can still take today: the time
        left until closing, multiplied by the staff serving in parallel,
        plus the daily buffer. None means no limit (the provider has not
        set opening/closing times)."""
        window = provider_day_window(provider, local_now)
        if window is None:
            return None
        open_dt, close_dt = window
        local_now = local_now or provider_local_now(provider)
        remaining = max(0.0, (close_dt - max(local_now, open_dt)).total_seconds() / 60)
        try:
            buffer_minutes = int(provider.get("bufferMinutes", 60))
        except (TypeError, ValueError):
            buffer_minutes = 60
        return int(remaining) * provider_staff_limit(provider) + max(0, min(240, buffer_minutes))

    def committed_minutes(provider, entries):
        """Minutes already promised: everyone being served plus the regular
        (non-buffer) waiting queue."""
        return sum(
            visit_minutes(provider, entry.get("service"))
            for entry in entries
            if entry.get("status") == "serving" or not entry.get("buffer")
        )

    def entry_fits_capacity(provider, entries, extra_minutes):
        limit = capacity_limit_minutes(provider)
        if limit is None:
            return True
        return committed_minutes(provider, entries) + extra_minutes <= limit

    def promote_buffer_entries(provider):
        """Move buffer-waitlist entries into the regular queue, in join
        order, while today's remaining capacity allows. Caller persists
        the provider record; changed entries are saved here."""
        if capacity_limit_minutes(provider) is None:
            return
        entries = active_provider_queues(provider["id"])
        committed = committed_minutes(provider, entries)
        limit = capacity_limit_minutes(provider)
        for entry in entries:
            if entry.get("status") != "waiting" or not entry.get("buffer"):
                continue
            minutes = visit_minutes(provider, entry.get("service"))
            if committed + minutes > limit:
                continue
            entry["buffer"] = False
            committed += minutes
            store.put("queues", entry["id"], entry, entry)
            log.info("queue.buffer_promoted queue=%s provider=%s", entry["id"], provider["id"])

    def apply_day_rollover(provider):
        """Start-of-day housekeeping, applied lazily on queue activity.

        Only runs once the provider has set opening/closing times. Waiting
        entries left over from earlier days are either carried into today
        (keeping their place at the front) or cleared into the dequeued
        list, per the provider's carry-forward setting. Must be called
        inside a store transaction; persists changed entries and, when
        anything changed, the provider record is left to the caller.
        """
        if provider_day_window(provider) is None:
            return False
        local_now = provider_local_now(provider)
        today = local_now.date()
        changed = False
        for entry in active_provider_queues(provider["id"]):
            if entry.get("status") != "waiting":
                continue
            try:
                joined_local = provider_local_now(
                    provider, datetime.fromisoformat(entry.get("createdAt", ""))
                )
            except (TypeError, ValueError):
                continue
            if joined_local.date() >= today:
                continue
            if provider.get("carryForwardQueue"):
                if entry.get("buffer") or not entry.get("carriedOver"):
                    entry["buffer"] = False
                    entry["carriedOver"] = True
                    store.put("queues", entry["id"], entry, entry)
                    changed = True
            else:
                entry["status"] = "no_show"
                entry["buffer"] = False
                entry["removedAt"] = datetime.now(timezone.utc).isoformat()
                entry["endedAt"] = entry["removedAt"]
                entry["removeReason"] = "day_end"
                store.put("queues", entry["id"], entry, entry)
                changed = True
                log.info("queue.day_cleared queue=%s provider=%s", entry["id"], provider["id"])
        if changed:
            promote_buffer_entries(provider)
        return changed

    def record_prediction_outcome(entry):
        """Log how the join-time prediction compared with the actual wait
        once the customer is called, for future estimate improvements."""
        predicted = entry.get("predictedWaitMinutes")
        started = entry.get("startedAt")
        created = entry.get("createdAt")
        if predicted is None or not started or not created:
            return
        try:
            actual = round(
                (datetime.fromisoformat(started) - datetime.fromisoformat(created)).total_seconds() / 60
            )
        except (TypeError, ValueError):
            return
        entry["actualWaitMinutes"] = actual
        log.info(
            "queue.prediction_outcome queue=%s provider=%s predicted_wait=%s actual_wait=%s error=%s",
            entry["id"],
            entry.get("providerId"),
            predicted,
            actual,
            actual - int(predicted),
        )

    def record_service_time(provider, entry):
        """Fold a completed visit's real duration into the measured
        averages used for predictions, and log it against the estimate."""
        started = entry.get("startedAt")
        completed = entry.get("completedAt")
        if not started or not completed:
            return
        try:
            duration = (datetime.fromisoformat(completed) - datetime.fromisoformat(started)).total_seconds() / 60
        except (TypeError, ValueError):
            return
        if duration <= 0:
            return
        estimated = visit_minutes(provider, entry.get("service"))
        names = service_names(entry.get("service")) or ["General service"]
        share = duration / len(names)

        def updated(raw, value):
            stats = cleaned_stats(raw) or {"avg": 0.0, "count": 0}
            total = stats["avg"] * stats["count"] + value
            return {"avg": total / (stats["count"] + 1), "count": stats["count"] + 1}

        provider["overallStats"] = updated(provider.get("overallStats"), share)
        if len(names) == 1:
            stats = dict(provider.get("serviceStats") or {})
            stats[names[0]] = updated(stats.get(names[0]), duration)
            provider["serviceStats"] = stats
        log.info(
            "queue.service_time queue=%s provider=%s service=%s actual_minutes=%.1f estimated_minutes=%s",
            entry["id"],
            provider["id"],
            entry.get("service"),
            duration,
            estimated,
        )

    def refresh_provider_metrics(provider):
        active = active_provider_queues(provider["id"])
        provider["people"] = len(active)
        provider["wait"] = total_wait_minutes(provider, active)
        return provider

    def provider_queue_view(provider, entries=None):
        if entries is None:
            entries = active_provider_queues(provider["id"])
        serving_minutes = sum(
            visit_minutes(provider, entry.get("service"))
            for entry in entries
            if entry.get("status") == "serving"
        )
        staff = provider_staff_limit(provider)
        ahead_minutes = 0
        now_utc = datetime.now(timezone.utc)
        visible_entries = []
        for entry in entries:
            customer_id = entry.get("customerId")
            customer = store.get("users", customer_id) if isinstance(customer_id, str) else None
            visible = {
                **entry,
                "customerName": customer.get("name", "Customer") if customer else entry.get("customerName", "Customer"),
                "customerEmail": customer.get("email") if customer else entry.get("email"),
                "customerPhone": customer.get("phone") if customer else entry.get("phone"),
                "customerAddress": entry.get("address"),
                "customerService": entry.get("service"),
                "duration": visit_minutes(provider, entry.get("service")),
                "buffer": bool(entry.get("buffer")),
                "carriedOver": bool(entry.get("carriedOver")),
            }
            if entry.get("status") == "serving":
                visible["estimatedWait"] = 0
            elif entry.get("status") == "waiting":
                visible["estimatedWait"] = -(-(serving_minutes + ahead_minutes) // staff)
                ahead_minutes += visible["duration"]
                turn = now_utc + timedelta(minutes=visible["estimatedWait"])
                visible["predictedTurnAt"] = turn.isoformat()
                visible["predictedTurnLabel"] = format_local_time(provider, turn)
            visible_entries.append(visible)
        return visible_entries

    def resolve_service_selection(provider, requested):
        """Resolve a customer's service selection against the provider's list.

        Accepts a list of names (or a single name). Unknown names are dropped
        and duplicates removed; when nothing valid remains, the provider's
        first service is the default, or "General service" when the provider
        lists none. Returns one comma-joined string for the queue record.
        """
        services = provider.get("services") or []
        if isinstance(requested, str):
            requested = [requested]
        selected = []
        if isinstance(requested, (list, tuple)):
            for item in requested:
                if isinstance(item, str) and item.strip() in services and item.strip() not in selected:
                    selected.append(item.strip())
        if not selected:
            selected = [services[0] if services else "General service"]
        return ", ".join(selected)

    def create_walk_in_entry(provider, body):
        if not isinstance(body, dict):
            raise ApiError(400, "Request body must be an object.")
        if set(body) - {"name", "email", "phone", "address", "service", "services"}:
            raise ApiError(400, "Only walk-in customer details can be submitted.")
        store.lock_providers([provider["id"]])
        provider = require_provider(str(provider["id"]))
        ensure_provider_approved(provider)
        if provider.get("status") != "Open":
            raise ApiError(409, "This provider is not accepting customers right now.")
        apply_day_rollover(provider)

        name = body.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ApiError(400, "Enter a customer name (up to 100 characters).")
        email_input = body.get("email", "")
        phone_input = body.get("phone", "")
        if not isinstance(email_input, str) or not isinstance(phone_input, str):
            raise ApiError(400, "Enter a valid email address or phone number.")
        email = normalized_email(email_input.strip()) if email_input.strip() else None
        phone = normalized_phone(phone_input.strip()) if phone_input.strip() else None
        if email_input.strip() and not email:
            raise ApiError(400, "Enter a valid email address.")
        if phone_input.strip() and not phone:
            raise ApiError(400, "Enter a valid international phone number, including the + country code.")
        if not email and not phone:
            raise ApiError(400, "Enter an email address, a phone number, or both.")

        address = body.get("address", "")
        if not isinstance(address, str) or len(address.strip()) > 200:
            raise ApiError(400, "Address must be no longer than 200 characters.")
        requested = body.get("services")
        if requested is None:
            requested = body.get("service")
        service = resolve_service_selection(provider, requested)

        active_queues = active_provider_queues(provider["id"])
        wait_minutes = total_wait_minutes(provider, active_queues)
        now_utc = datetime.now(timezone.utc)
        is_buffer = not entry_fits_capacity(
            provider, active_queues, visit_minutes(provider, service)
        )
        entry = {
            "id": str(uuid.uuid4()),
            "customerId": None,
            "providerId": provider["id"],
            "customerName": name.strip(),
            "email": email,
            "phone": phone,
            "address": address.strip(),
            "service": service.strip(),
            "source": "walk-in",
            "peopleAhead": len(active_queues),
            "wait": wait_minutes,
            "status": "waiting",
            "buffer": is_buffer,
            "predictedWaitMinutes": wait_minutes,
            "predictedTurnAt": (now_utc + timedelta(minutes=wait_minutes)).isoformat(),
            "createdAt": now_utc.isoformat(),
        }
        store.create("queues", entry["id"], entry, entry)
        log.info(
            "queue.walk_in queue=%s provider=%s service=%s",
            entry["id"],
            provider["id"],
            entry["service"],
        )
        log.info(
            "queue.prediction queue=%s provider=%s predicted_wait=%s buffer=%s",
            entry["id"],
            provider["id"],
            wait_minutes,
            is_buffer,
        )
        refresh_provider_metrics(provider)
        store.put("providers", str(provider["id"]), provider, provider)
        return entry

    def parse_services(value):
        services = [item.strip() for item in value.split(",") if item.strip()]
        if len(services) > 20 or any(len(service) > 60 for service in services):
            return None
        return services

    def normalized_email(value):
        if not isinstance(value, str) or not value:
            return None
        _, parsed_email = parseaddr(value)
        if (
            len(value) > 254
            or parsed_email != value
            or "@" not in value
            or "." not in value.rsplit("@", 1)[-1]
        ):
            return None
        return value.casefold()

    def normalized_phone(value):
        if not isinstance(value, str) or not value:
            return None
        if not PHONE_INPUT.fullmatch(value) or value.count("+") > 1:
            return None
        digits = re.sub(r"\D", "", value)
        if value.startswith("+"):
            phone = "+" + digits
        elif len(digits) == 10:
            phone = "+91" + digits
        elif len(digits) == 11 and digits.startswith("0"):
            phone = "+91" + digits[1:]
        else:
            return None
        return phone if PHONE_E164.fullmatch(phone) else None

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
        return {
            "current_user": current_user(),
            "csrf_token": csrf_token,
            "admin_authenticated": bool(app.config["ADMIN_PASSWORD"] and session.get("admin_authenticated") is True),
        }

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

    def photo_record_id(photo_id):
        if isinstance(photo_id, str) and photo_id.endswith(".jpg"):
            return photo_id[:-4]
        return photo_id

    def save_provider_photo(provider_id, photo_id, contents):
        record = {
            "id": photo_record_id(photo_id),
            "providerId": str(provider_id),
            "data": base64.b64encode(contents).decode("ascii"),
            "contentType": "image/jpeg",
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        index_entry = {
            "id": record["id"], "providerId": record["providerId"],
            "contentType": record["contentType"], "createdAt": record["createdAt"],
        }
        try:
            store.put("provider_photos", record["id"], record, index_entry)
            return
        except Exception:
            log.warning("Provider photo could not be saved to the database; using file storage.", exc_info=True)
        photo_path = Path(app.config["DATA_DIR"]) / "provider_photos" / photo_id
        photo_path.parent.mkdir(parents=True, exist_ok=True)
        photo_path.write_bytes(contents)

    def load_provider_photo(photo_id):
        try:
            record = store.get("provider_photos", photo_record_id(photo_id))
        except Exception:
            record = None
        if record and record.get("data"):
            try:
                return base64.b64decode(record["data"])
            except (ValueError, TypeError):
                pass
        photo_path = Path(app.config["DATA_DIR"]) / "provider_photos" / photo_id
        if photo_path.is_file():
            return photo_path.read_bytes()
        return None

    def delete_provider_photo(photo_id):
        try:
            store.delete("provider_photos", photo_record_id(photo_id))
        except Exception:
            pass
        photo_path = Path(app.config["DATA_DIR"]) / "provider_photos" / photo_id
        photo_path.unlink(missing_ok=True)

    @app.get("/media/providers/<photo_id>")
    def provider_photo(photo_id):
        if not re.fullmatch(r"[a-f0-9]{32}(\.jpg)?", photo_id):
            abort(404)
        contents = load_provider_photo(photo_id)
        if contents is None:
            abort(404)
        response = make_response(contents)
        response.mimetype = "image/jpeg"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    # Branding images ship as base64 text files (the repo tooling stores
    # text only), decoded once at startup and served from memory.
    branding_assets = {}
    for asset_name in ("logo.png", "logo-icon.png", "favicon.png"):
        try:
            encoded = (Path(app.static_folder) / f"{asset_name}.b64").read_text(encoding="ascii")
            branding_assets[asset_name] = base64.b64decode(encoded)
        except (OSError, ValueError):
            app.logger.warning("Branding asset %s could not be loaded", asset_name)

    @app.get("/branding/<asset_name>")
    def branding_asset(asset_name):
        payload = branding_assets.get(asset_name)
        if payload is None:
            abort(404)
        response = make_response(payload)
        response.mimetype = "image/png"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "public, max-age=3600"
        return response

    # ---- Advertisements -------------------------------------------------
    # Ads are configured by admins, targeted by the existing provider
    # categories, and shown by one reusable client component in two
    # placements (home hero, provider dashboard under the QR code). Media
    # lives in the ad_media store collection (base64), falling back to
    # files under DATA_DIR, for the same reason provider photos do: the
    # production filesystem is ephemeral. Uploads are capped (images
    # 5 MB, videos 10 MB) so nothing large is stored.

    def parse_ad_instant(value):
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def ad_schedule_open(ad, now):
        starts = parse_ad_instant(ad.get("startsAt"))
        if starts is not None and now < starts:
            return False
        ends = parse_ad_instant(ad.get("endsAt"))
        if ends is not None and now >= ends:
            return False
        return True

    def public_ad(ad):
        result = {
            "id": ad["id"],
            "contentType": ad.get("contentType"),
            "durationSeconds": ad.get("durationSeconds", 8),
            "categories": ad.get("categories", []),
        }
        if ad.get("contentType") == "text":
            result["textContent"] = ad.get("textContent", "")
        elif ad.get("mediaId"):
            result["mediaUrl"] = url_for("ad_media_file", media_id=ad["mediaId"])
        return result

    def eligible_ads(placement, category=None, provider=None):
        if placement == "dashboard":
            if provider is None:
                return []
            if provider.get("adsEnabled", True) is False:
                return []
            category = provider.get("category")
        try:
            all_ads = store.list("advertisements")
        except Exception:
            # If the ads tables are not ready yet, the safe behavior is
            # to show no advertisements at all.
            log.warning("Advertisement list failed; serving no ads.", exc_info=True)
            return []
        now = datetime.now(timezone.utc)
        matches = []
        for ad in all_ads:
            if ad.get("active") is not True:
                continue
            if placement not in (ad.get("placements") or []):
                continue
            if category and category not in (ad.get("categories") or []):
                continue
            if not ad_schedule_open(ad, now):
                continue
            if ad.get("contentType") in {"image", "video"} and not ad.get("mediaId"):
                continue
            if ad.get("contentType") == "text" and not (ad.get("textContent") or "").strip():
                continue
            matches.append(ad)
        matches.sort(key=lambda ad: (-(ad.get("priority") or 0), ad.get("createdAt", ""), str(ad.get("id"))))
        return matches

    def cleaned_ad_fields(source, existing=None):
        def raw(key, default=None):
            if source is not None and key in source:
                return source.get(key)
            if existing is not None:
                return existing.get(key, default)
            return default

        name = raw("name", "")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ApiError(400, "Advertisement name must be 1 to 120 characters.")
        description = raw("description", "") or ""
        if not isinstance(description, str) or len(description.strip()) > 500:
            raise ApiError(400, "Description must be 500 characters or fewer.")
        content_type = raw("contentType", "")
        if content_type not in AD_CONTENT_TYPES:
            raise ApiError(400, "Choose a content type: image, video, or text.")
        text_content = raw("textContent", "") or ""
        if not isinstance(text_content, str) or len(text_content.strip()) > 500:
            raise ApiError(400, "Text content must be 500 characters or fewer.")
        if content_type == "text" and not text_content.strip():
            raise ApiError(400, "Text advertisements need text content.")
        duration = raw("durationSeconds", 8)
        if isinstance(duration, bool):
            raise ApiError(400, "Display duration must be a whole number of seconds.")
        try:
            duration = int(duration)
        except (TypeError, ValueError):
            raise ApiError(400, "Display duration must be a whole number of seconds.")
        if not 3 <= duration <= 120:
            raise ApiError(400, "Display duration must be between 3 and 120 seconds.")

        def cleaned_list(value, allowed, label):
            if isinstance(value, str):
                try:
                    value = json.loads(value)
                except ValueError:
                    value = [part.strip() for part in value.split(",") if part.strip()]
            if (
                not isinstance(value, list)
                or not value
                or any(item not in allowed for item in value)
            ):
                raise ApiError(400, label)
            return list(dict.fromkeys(value))

        categories = cleaned_list(
            raw("categories", []), PROVIDER_CATEGORIES,
            "Choose at least one valid service category.",
        )
        placements = cleaned_list(
            raw("placements", []), AD_PLACEMENTS,
            "Choose at least one placement: home or dashboard.",
        )
        starts_at = raw("startsAt") or None
        ends_at = raw("endsAt") or None
        starts = parse_ad_instant(starts_at) if starts_at else None
        ends = parse_ad_instant(ends_at) if ends_at else None
        if starts_at and starts is None:
            raise ApiError(400, "Start date is not a valid date and time.")
        if ends_at and ends is None:
            raise ApiError(400, "End date is not a valid date and time.")
        if starts and ends and ends <= starts:
            raise ApiError(400, "End date must be after the start date.")
        priority = raw("priority", 0)
        if isinstance(priority, bool):
            raise ApiError(400, "Priority must be a whole number.")
        try:
            priority = int(priority)
        except (TypeError, ValueError):
            raise ApiError(400, "Priority must be a whole number.")
        if not 0 <= priority <= 100:
            raise ApiError(400, "Priority must be between 0 and 100.")
        active = raw("active", True)
        if isinstance(active, str):
            active = active.strip().lower() in {"true", "1", "yes", "on"}
        return {
            "name": name.strip(),
            "description": description.strip(),
            "contentType": content_type,
            "textContent": text_content.strip(),
            "durationSeconds": duration,
            "categories": categories,
            "placements": placements,
            "startsAt": starts.isoformat() if starts else None,
            "endsAt": ends.isoformat() if ends else None,
            "priority": priority,
            "active": bool(active),
        }

    def process_ad_upload(upload, content_type):
        """Validate an ad media upload; returns (media dict, error)."""
        filename = upload.filename or ""
        extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
        if content_type == "image":
            if extension not in {"jpg", "jpeg", "png", "webp"}:
                return None, "Upload a JPG, PNG, or WebP image."
            contents = upload.stream.read(MAX_UPLOAD_BYTES + 1)
            if len(contents) > MAX_UPLOAD_BYTES:
                return None, "Choose an image smaller than 5 MB."
            try:
                with Image.open(io.BytesIO(contents)) as image:
                    image_format = image.format
                    if image_format not in SUPPORTED_IMAGE_FORMATS:
                        return None, "Upload a JPEG, PNG, or WebP image."
                    if image.width * image.height > MAX_IMAGE_PIXELS:
                        return None, "Choose an image with dimensions under 20 megapixels."
                    image.verify()
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
                return None, "The selected file is not a valid supported image."
            mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}[image_format]
            return {"contents": contents, "contentType": mime, "fileName": filename, "fileSize": len(contents)}, None
        if content_type == "video":
            if extension not in {"mp4", "webm"}:
                return None, "Upload an MP4 or WebM video."
            contents = upload.stream.read(MAX_AD_VIDEO_BYTES + 1)
            if len(contents) > MAX_AD_VIDEO_BYTES:
                return None, "Choose a video smaller than 10 MB."
            if extension == "mp4" and contents[4:8] != b"ftyp":
                return None, "The selected file is not a valid MP4 video."
            if extension == "webm" and not contents.startswith(b"\x1a\x45\xdf\xa3"):
                return None, "The selected file is not a valid WebM video."
            mime = "video/mp4" if extension == "mp4" else "video/webm"
            return {"contents": contents, "contentType": mime, "fileName": filename, "fileSize": len(contents)}, None
        return None, "Text advertisements do not use a file upload."

    def save_ad_media(ad_id, contents, content_type, file_name):
        media_id = uuid.uuid4().hex
        record = {
            "id": media_id,
            "adId": str(ad_id),
            "data": base64.b64encode(contents).decode("ascii"),
            "contentType": content_type,
            "fileName": file_name,
            "fileSize": len(contents),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        index_entry = {
            "id": media_id, "adId": record["adId"], "contentType": content_type,
            "fileName": file_name, "fileSize": len(contents), "createdAt": record["createdAt"],
        }
        try:
            store.put("ad_media", media_id, record, index_entry)
            return media_id
        except Exception:
            log.warning("Ad media could not be saved to the database; using file storage.", exc_info=True)
        media_dir = Path(app.config["DATA_DIR"]) / "ad_media"
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / media_id).write_bytes(contents)
        (media_dir / f"{media_id}.json").write_text(
            json.dumps({"contentType": content_type, "fileName": file_name}), encoding="utf-8"
        )
        return media_id

    def load_ad_media(media_id):
        if not isinstance(media_id, str) or not re.fullmatch(r"[a-f0-9]{32}", media_id):
            return None, None
        try:
            record = store.get("ad_media", media_id)
        except Exception:
            record = None
        if record and record.get("data"):
            try:
                return base64.b64decode(record["data"]), record.get("contentType") or "application/octet-stream"
            except (ValueError, TypeError):
                pass
        media_dir = Path(app.config["DATA_DIR"]) / "ad_media"
        media_path = media_dir / media_id
        if media_path.is_file():
            content_type = "application/octet-stream"
            sidecar = media_dir / f"{media_id}.json"
            if sidecar.is_file():
                try:
                    content_type = json.loads(sidecar.read_text(encoding="utf-8")).get("contentType", content_type)
                except (ValueError, OSError):
                    pass
            return media_path.read_bytes(), content_type
        return None, None

    def delete_ad_media(media_id):
        if not isinstance(media_id, str) or not re.fullmatch(r"[a-f0-9]{32}", media_id):
            return
        try:
            store.delete("ad_media", media_id)
        except Exception:
            pass
        media_dir = Path(app.config["DATA_DIR"]) / "ad_media"
        (media_dir / media_id).unlink(missing_ok=True)
        (media_dir / f"{media_id}.json").unlink(missing_ok=True)

    @app.get("/media/ads/<media_id>")
    def ad_media_file(media_id):
        contents, content_type = load_ad_media(media_id)
        if contents is None:
            abort(404)
        response = send_file(
            io.BytesIO(contents), mimetype=content_type, conditional=True, max_age=3600
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/api/ads/eligible")
    def ads_eligible():
        placement = request.args.get("placement", "")
        if placement not in AD_PLACEMENTS:
            raise ApiError(400, "Placement must be home or dashboard.")
        category = request.args.get("category") or None
        if category is not None and category not in PROVIDER_CATEGORIES:
            raise ApiError(400, "Choose a valid service category.")
        provider = None
        if placement == "dashboard":
            provider_id = request.args.get("providerId", "")
            if not CLIENT_ID.fullmatch(provider_id):
                raise ApiError(400, "A valid provider ID is required.")
            provider = store.get("providers", provider_id)
            if provider is None:
                raise ApiError(404, "Provider not found.")
        ads = eligible_ads(placement, category=category, provider=provider)
        return jsonify(ads=[public_ad(ad) for ad in ads])

    @app.post("/api/ads/events")
    def ads_events():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        if body.get("event") not in AD_EVENT_TYPES:
            raise ApiError(400, "Unknown advertisement event.")
        if body.get("placement") not in AD_PLACEMENTS:
            raise ApiError(400, "Placement must be home or dashboard.")
        ad_id = body.get("adId")
        ad = None
        if isinstance(ad_id, str) and CLIENT_ID.fullmatch(ad_id):
            try:
                ad = store.get("advertisements", ad_id)
            except Exception:
                ad = None
        if ad is None:
            raise ApiError(404, "Advertisement not found.")
        category = body.get("category")
        if category is not None and category not in PROVIDER_CATEGORIES:
            category = None
        provider_id = body.get("providerId")
        if not isinstance(provider_id, str) or not CLIENT_ID.fullmatch(provider_id):
            provider_id = None
        record = {
            "id": str(uuid.uuid4()),
            "adId": ad["id"],
            "event": body["event"],
            "placement": body["placement"],
            "category": category,
            "providerId": provider_id,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        try:
            store.create("ad_events", record["id"], record, record)
        except Exception:
            log.warning("Advertisement event could not be recorded.", exc_info=True)
        return "", 204

    @app.get("/api/admin/ads")
    def admin_ads_list():
        require_admin()
        status = request.args.get("status") or None
        category = request.args.get("category") or None
        content_type = request.args.get("type") or None
        ads = store.list("advertisements")
        ads.sort(key=lambda ad: (ad.get("createdAt", ""), str(ad.get("id"))), reverse=True)
        result = []
        for ad in ads:
            if status == "active" and ad.get("active") is not True:
                continue
            if status == "inactive" and ad.get("active") is True:
                continue
            if category and category not in (ad.get("categories") or []):
                continue
            if content_type and ad.get("contentType") != content_type:
                continue
            result.append(dict(ad))
        return jsonify(ads=result)

    @app.post("/api/admin/ads")
    def admin_ads_create():
        require_admin()
        source = request.form if request.form else (request.get_json(silent=True) or {})
        fields = cleaned_ad_fields(source)
        upload = request.files.get("media")
        media = None
        if fields["contentType"] in {"image", "video"}:
            if not upload or not upload.filename:
                raise ApiError(400, "Choose a media file for this advertisement.")
            media, error = process_ad_upload(upload, fields["contentType"])
            if error:
                raise ApiError(400, error)
        now = datetime.now(timezone.utc).isoformat()
        ad = {
            "id": str(uuid.uuid4()),
            **fields,
            "mediaId": None,
            "createdAt": now,
            "updatedAt": now,
        }
        store.create("advertisements", ad["id"], ad, ad)
        if media is not None:
            # The advertisement row must exist before its media: ad_media
            # references it with a foreign key, so saving media first fails
            # on Postgres and silently falls back to ephemeral file storage.
            ad["mediaId"] = save_ad_media(ad["id"], media["contents"], media["contentType"], media["fileName"])
            ad["mediaFileName"] = media["fileName"]
            ad["mediaFileSize"] = media["fileSize"]
            store.put("advertisements", ad["id"], ad, ad)
        log.info("ads.create ad=%s type=%s", ad["id"], ad["contentType"])
        return jsonify(ad=dict(ad)), 201

    @app.patch("/api/admin/ads/<ad_id>")
    def admin_ads_update(ad_id):
        require_admin()
        if not CLIENT_ID.fullmatch(ad_id or ""):
            raise ApiError(404, "Advertisement not found.")
        ad = store.get("advertisements", ad_id)
        if ad is None:
            raise ApiError(404, "Advertisement not found.")
        source = request.form if request.form else (request.get_json(silent=True) or {})
        fields = cleaned_ad_fields(source, existing=ad)
        upload = request.files.get("media")
        has_upload = bool(upload and upload.filename)
        if (
            fields["contentType"] in {"image", "video"}
            and fields["contentType"] != ad.get("contentType")
            and not has_upload
        ):
            raise ApiError(400, "Upload a media file for the new content type.")
        updated = {**ad, **fields, "updatedAt": datetime.now(timezone.utc).isoformat()}
        old_media_id = ad.get("mediaId")
        new_media_id = old_media_id
        if has_upload and fields["contentType"] in {"image", "video"}:
            media, error = process_ad_upload(upload, fields["contentType"])
            if error:
                raise ApiError(400, error)
            new_media_id = save_ad_media(ad["id"], media["contents"], media["contentType"], media["fileName"])
            updated["mediaFileName"] = media["fileName"]
            updated["mediaFileSize"] = media["fileSize"]
        if fields["contentType"] == "text":
            new_media_id = None
            updated.pop("mediaFileName", None)
            updated.pop("mediaFileSize", None)
        updated["mediaId"] = new_media_id
        store.put("advertisements", ad["id"], updated, updated)
        if old_media_id and old_media_id != new_media_id:
            delete_ad_media(old_media_id)
        log.info("ads.update ad=%s", ad["id"])
        return jsonify(ad=dict(updated))

    @app.delete("/api/admin/ads/<ad_id>")
    def admin_ads_delete(ad_id):
        require_admin()
        if not CLIENT_ID.fullmatch(ad_id or ""):
            raise ApiError(404, "Advertisement not found.")
        ad = store.get("advertisements", ad_id)
        if ad is None:
            raise ApiError(404, "Advertisement not found.")
        store.delete("advertisements", ad["id"])
        if ad.get("mediaId"):
            delete_ad_media(ad["mediaId"])
        log.info("ads.delete ad=%s", ad["id"])
        return jsonify(ok=True)

    @app.patch("/api/admin/providers/<provider_id>/ads-permission")
    def update_provider_ads_permission(provider_id):
        require_admin()
        body = request.get_json(silent=True)
        if (
            not isinstance(body, dict)
            or set(body) - {"adsEnabled"}
            or not isinstance(body.get("adsEnabled"), bool)
        ):
            raise ApiError(400, "Provide adsEnabled as true or false.")
        with store.transaction():
            store.lock_providers([provider_id])
            provider = require_provider(provider_id)
            provider["adsEnabled"] = body["adsEnabled"]
            store.put("providers", str(provider["id"]), provider, provider)
            log.info("ads.permission provider=%s enabled=%s", provider["id"], body["adsEnabled"])
            return jsonify(provider=provider)

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
        log.log(
            logging.WARNING if error.status >= 500 else logging.INFO,
            "api error status=%s method=%s path=%s message=%s",
            error.status,
            request.method,
            request.path,
            error,
        )
        return jsonify(error=str(error)), error.status

    @app.errorhandler(500)
    def handle_unexpected_error(error):
        log.error("unhandled error method=%s path=%s", request.method, request.path, exc_info=error)
        return jsonify(error="Something went wrong. Please try again."), 500

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
        with store.transaction():
            for raw_provider in store.list("providers"):
                store.lock_providers([str(raw_provider["id"])])
                fresh = store.get("providers", str(raw_provider["id"]))
                if fresh and apply_day_rollover(fresh):
                    fresh = refresh_provider_metrics(fresh)
                    store.put("providers", str(fresh["id"]), fresh, fresh)
        all_providers = [provider_with_contact(provider) for provider in store.list("providers")]
        providers = [
            provider for provider in all_providers
            if provider_approval_status(provider) == "approved"
            and not provider_is_private(provider)
        ]
        for listed in providers:
            opening = parse_hhmm(listed.get("openTime"))
            closing = parse_hhmm(listed.get("closeTime"))
            if opening and closing:
                listed["hours"] = f"{format_hhmm(opening)} – {format_hhmm(closing)}"
        my_queues = []
        reviewable_visits = []
        user = current_user()
        if user and user.get("role") == "customer":
            providers_by_id = {str(provider["id"]): provider for provider in all_providers}
            for entry in store.list("queues"):
                if entry.get("customerId") != user["id"] or entry.get("status") not in {"waiting", "serving"}:
                    continue
                provider_for_queue = providers_by_id.get(str(entry["providerId"]))
                if not provider_for_queue:
                    continue
                active = active_provider_queues(entry["providerId"])
                position = next((index for index, item in enumerate(active) if item["id"] == entry["id"]), None)
                wait = total_wait_minutes(provider_for_queue, active[:position]) if position is not None else 0
                my_queues.append({
                    "queue": {**entry, "wait": wait},
                    "provider": provider_for_queue,
                    "position": "Now" if entry.get("status") == "serving" else ((position + 1) if position is not None else "—"),
                    "wait": wait,
                    "buffer": bool(entry.get("buffer")),
                    "predictedTurnLabel": format_local_time(
                        provider_for_queue, datetime.now(timezone.utc) + timedelta(minutes=wait)
                    ) if entry.get("status") == "waiting" else None,
                })
            my_queues.sort(key=lambda item: item["queue"].get("createdAt", ""))
            reviews_by_visit = {
                str(review.get("visitId")): review
                for review in all_customer_reviews()
                if str(review.get("customerId")) == str(user["id"])
            }
            completed = [
                entry
                for entry in store.list("queues")
                if entry.get("customerId") == user["id"]
                and entry.get("status") == "completed"
                and not entry.get("reviewDismissed")
            ]
            completed.sort(
                key=lambda entry: entry.get("completedAt") or entry.get("createdAt", ""),
                reverse=True,
            )
            for entry in completed[:5]:
                provider_for_queue = providers_by_id.get(str(entry["providerId"]))
                if not provider_for_queue:
                    continue
                reviewable_visits.append({
                    "queue": entry,
                    "provider": provider_for_queue,
                    "review": reviews_by_visit.get(str(entry["id"])),
                })
        joined_ids = {str(item["queue"]["providerId"]) for item in my_queues}
        view = request.args.get("view", "customer")
        if view not in {"customer", "provider", "admin"}:
            view = "customer"
        if view == "admin":
            if not app.config["ADMIN_PASSWORD"]:
                return render_template("admin_login.html", error="Admin access is not configured. Set SMARTQUEUE_ADMIN_PASSWORD and restart the app."), 503
            if session.get("admin_authenticated") is not True:
                return redirect(url_for("admin_login"))
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
            provider = provider_with_contact(provider)
            provider["approvalStatus"] = provider_approval_status(provider)
            provider_queue = provider_queue_view(provider)
            serving_slots_full = (
                sum(1 for entry in provider_queue if entry.get("status") == "serving")
                >= provider_staff_limit(provider)
            )
            provider = refresh_provider_metrics(provider)
        if view == "admin":
            for item in all_providers:
                item["approvalStatus"] = provider_approval_status(item)
        return render_template(
            "index.html",
            providers=providers,
            my_queues=my_queues,
            reviewable_visits=reviewable_visits,
            joined_ids=joined_ids,
            view=view,
            provider=provider,
            provider_queue=provider_queue,
            serving_slots_full=serving_slots_full if view == "provider" else False,
            admin_providers=all_providers if view == "admin" else [],
            provider_categories=PROVIDER_CATEGORIES,
            provider_timezones=PROVIDER_TIMEZONES,
            favorite_ids={str(item) for item in user.get("favoriteProviderIds", [])} if user and user.get("role") == "customer" else set(),
        )

    @app.get("/how-to-use")
    def how_to_use():
        return render_template("how_to_use.html")

    @app.get("/contact")
    def contact():
        return render_template("contact.html")

    @app.get("/privacy-terms")
    def privacy_terms():
        return render_template("privacy_terms.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "GET":
            return render_template("login.html", error=None)
        identifier = request.form.get("identifier", "").strip()
        email_identifier = identifier.casefold()
        phone_identifier = normalized_phone(identifier) or ""
        user = next(
            (item for item in store.list("users")
             if (email_identifier and (item.get("email") or "").casefold() == email_identifier)
             or (phone_identifier and item.get("phone") == phone_identifier)),
            None,
        )
        record = store.get("users", user["id"]) if user else None
        if not record or not check_password_hash(record.get("passwordHash", ""), request.form.get("password", "")):
            log.info("auth.login_failed")
            return render_template("login.html", error="Email/phone or password is incorrect."), 401
        session.clear()
        session["user_id"] = record["id"]
        log.info("auth.login user=%s role=%s", record["id"], record["role"])
        session["csrf_token"] = secrets.token_urlsafe(32)
        destination = url_for("home", view="provider") if record["role"] == "provider" else url_for("home")
        return redirect(destination, code=303)

    @app.route("/reset-password", methods=["GET", "POST"])
    def reset_password():
        if request.method == "GET":
            return render_template("reset_password.html", error=None, success=False)

        email = request.form.get("email", "").strip()
        phone_input = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not normalized_email(email):
            return render_template(
                "reset_password.html",
                error="Enter a valid email address.",
                success=False,
                form={"email": email, "phone": phone_input},
            ), 400
        phone = normalized_phone(phone_input)
        if not phone:
            return render_template(
                "reset_password.html",
                error="Enter a valid international phone number, including the + country code.",
                success=False,
                form={"email": email, "phone": phone_input},
            ), 400
        if len(password) < 10 or len(password) > 128:
            return render_template(
                "reset_password.html",
                error="Use a password between 10 and 128 characters.",
                success=False,
                form={"email": email, "phone": phone_input},
            ), 400
        if password != confirm_password:
            return render_template(
                "reset_password.html",
                error="The passwords do not match.",
                success=False,
                form={"email": email, "phone": phone_input},
            ), 400

        with store.transaction():
            account_index = next(
                (
                    account for account in store.list("users")
                    if (account.get("email") or "").casefold() == email.casefold()
                    and account.get("phone") == phone
                ),
                None,
            )
            if account_index:
                account = store.get("users", account_index["id"])
                if account:
                    account["passwordHash"] = generate_password_hash(password)
                    public_account = {
                        key: value for key, value in account.items()
                        if key != "passwordHash"
                    }
                    store.put("users", account["id"], account, public_account)

        return render_template("reset_password.html", error=None, success=True)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("home"), code=303)

    @app.route("/admin/login", methods=["GET", "POST"])
    def admin_login():
        if not app.config["ADMIN_PASSWORD"]:
            return render_template(
                "admin_login.html",
                error="Admin access is not configured. Set SMARTQUEUE_ADMIN_PASSWORD and restart the app.",
            ), 503
        if request.method == "GET":
            return render_template("admin_login.html", error=None)
        supplied = request.form.get("password", "")
        if not secrets.compare_digest(supplied, app.config["ADMIN_PASSWORD"]):
            return render_template("admin_login.html", error="Administrator password is incorrect."), 401
        session.clear()
        session["admin_authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(32)
        return redirect(url_for("home", view="admin"), code=303)

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
        confirm_password = request.form.get("confirm_password", "")
        errors = {}
        normalized_phone_value = ""
        if form["email"]:
            if not normalized_email(form["email"]):
                errors["email"] = "Enter a valid email address."
        if form["phone"]:
            normalized_phone_value = normalized_phone(form["phone"])
            if not normalized_phone_value:
                errors["phone"] = "Enter a valid international phone number, including the + country code."
        if not form["email"] and not form["phone"]:
            errors["contact"] = "Enter an email address, a phone number, or both."
        if not form["name"] or len(form["name"]) > 100:
            errors["name"] = "Enter your name (up to 100 characters)."
        if len(password) < 10 or len(password) > 128:
            errors["password"] = "Use a password between 10 and 128 characters."
        if password != confirm_password:
            errors["confirm_password"] = "The passwords do not match."
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
            if normalized_phone_value and any(user.get("phone") == normalized_phone_value for user in users):
                errors["phone"] = "An account with this phone number already exists."
            if errors:
                return render_registration(form, errors, status=409, photo_staged=staged_photo_path is not None)

            user_id = str(uuid.uuid4())
            user = {
                "id": user_id,
                "name": form["name"],
                "email": form["email"] or None,
                "phone": normalized_phone_value or None,
                "role": form["role"],
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
            provider = None
            photo_id = f"{uuid.uuid4().hex}.jpg" if photo_contents else None

            if form["role"] == "provider":
                providers = store.list("providers")
                provider_id = (
                    str(uuid.uuid4())
                    if app.config["POSTGRES_ENABLED"]
                    else str(max((int(item["id"]) for item in providers), default=0) + 1)
                )
                accent, icon = PROVIDER_CATEGORIES[form["category"]]
                provider = {
                    "id": provider_id if app.config["POSTGRES_ENABLED"] else int(provider_id),
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
                    "timezone": DEFAULT_PROVIDER_TIMEZONE,
                    "bufferMinutes": 60,
                    "carryForwardQueue": False,
                    "staffCount": 1,
                    "privateListing": False,
                    "accent": accent,
                    "icon": icon,
                    "services": services,
                    "contactEmail": form["email"] or None,
                    "contactPhone": normalized_phone_value or None,
                    "approvalStatus": "pending",
                    "approvalNote": "",
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
            log.info("auth.registered user=%s role=%s", user_id, form["role"])
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

        if provider is not None and photo_id:
            save_provider_photo(str(provider["id"]), photo_id, photo_contents)
        clear_registration_photo_draft()
        return redirect(url_for("register", created="1"), code=303)

    @app.get("/api/providers")
    def list_providers():
        return jsonify([
            provider_with_contact(provider)
            for provider in store.list("providers")
            if provider_approval_status(provider) == "approved"
            and not provider_is_private(provider)
        ])

    @app.get("/providers/<provider_id>/qr.svg")
    def provider_qr(provider_id):
        provider = require_provider(provider_id)
        ensure_provider_approved(provider)
        target_url = url_for("public_walk_in", provider_id=provider["id"], _external=True)
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(target_url)
        qr.make(fit=True)
        image = qr.make_image(image_factory=SvgPathImage)
        output = BytesIO()
        image.save(output)
        response = make_response(output.getvalue())
        response.headers["Content-Type"] = "image/svg+xml"
        response.headers["Cache-Control"] = "public, max-age=3600"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/providers/<provider_id>/walk-in", methods=["GET", "POST"])
    def public_walk_in(provider_id):
        provider = require_provider(provider_id)
        ensure_provider_approved(provider)
        if request.method == "GET":
            return render_template(
                "walk_in.html",
                provider=provider,
                success=request.args.get("joined") in {"1", "buffer"},
                buffer=request.args.get("joined") == "buffer",
                error=None,
            )
        try:
            with store.transaction():
                provider = require_provider(provider_id)
                body = request.form.to_dict()
                body.pop("csrf_token", None)
                selected_services = request.form.getlist("services")
                if selected_services:
                    body["services"] = selected_services
                entry = create_walk_in_entry(provider, body)
        except ApiError as error:
            return render_template(
                "walk_in.html",
                provider=provider,
                success=False,
                buffer=False,
                error=str(error),
            ), error.status
        return redirect(
            url_for(
                "public_walk_in",
                provider_id=provider_id,
                joined="buffer" if entry.get("buffer") else "1",
            ),
            code=303,
        )

    @app.get("/api/providers/<provider_id>/queues")
    def list_provider_queues(provider_id):
        _, provider = require_owned_provider(provider_id)
        queue_status = request.args.get("status", "active")
        if queue_status in {"active", "dequeued"}:
            with store.transaction():
                store.lock_providers([provider_id])
                provider = require_provider(provider_id)
                if apply_day_rollover(provider):
                    refresh_provider_metrics(provider)
                    store.put("providers", str(provider["id"]), provider, provider)
        if queue_status == "active":
            entries = active_provider_queues(provider_id)
        elif queue_status == "completed":
            entries = sorted(
                (
                    entry for entry in store.list("queues")
                    if str(entry.get("providerId")) == str(provider_id)
                    and entry.get("status") == "completed"
                ),
                key=lambda entry: entry.get("completedAt", ""),
                reverse=True,
            )
        elif queue_status == "dequeued":
            entries = sorted(
                (
                    entry for entry in store.list("queues")
                    if str(entry.get("providerId")) == str(provider_id)
                    and entry.get("status") == "no_show"
                ),
                key=lambda entry: entry.get("removedAt", ""),
                reverse=True,
            )
        else:
            raise ApiError(400, "Choose active, completed, or dequeued services.")
        return jsonify(queues=provider_queue_view(provider, entries))

    def provider_report_data(provider, period, entries):
        local_now = provider_local_now(provider)
        if period == "day":
            start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            end_local = start_local + timedelta(days=1)
            label = f"Today · {start_local.strftime('%d %b %Y')}"
        elif period == "week":
            start_local = (local_now - timedelta(days=local_now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            end_local = start_local + timedelta(days=7)
            label = (
                f"This week · {start_local.strftime('%d %b')} – "
                f"{(end_local - timedelta(days=1)).strftime('%d %b %Y')}"
            )
        elif period == "month":
            start_local = local_now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end_local = (start_local + timedelta(days=32)).replace(day=1)
            label = local_now.strftime("%B %Y")
        else:
            start_local = end_local = None
            label = "All time"
        start_utc = start_local.astimezone(timezone.utc) if start_local else None
        end_utc = end_local.astimezone(timezone.utc) if end_local else None

        def in_window(value):
            if not isinstance(value, str) or not value:
                return False
            try:
                moment = datetime.fromisoformat(value)
            except ValueError:
                return False
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            if start_utc and moment < start_utc:
                return False
            if end_utc and moment >= end_utc:
                return False
            return True

        completed = [
            entry for entry in entries
            if entry.get("status") == "completed" and in_window(entry.get("completedAt"))
        ]
        service_counts = {}
        service_durations = {}
        durations = []
        waits = []
        for entry in completed:
            names = service_names(entry.get("service"))
            for name in names:
                service_counts[name] = service_counts.get(name, 0) + 1
            started, finished, created = entry.get("startedAt"), entry.get("completedAt"), entry.get("createdAt")
            if not (started and finished):
                continue
            try:
                duration = (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds() / 60
            except ValueError:
                continue
            if duration < 0:
                continue
            durations.append(duration)
            if len(names) == 1:
                service_durations.setdefault(names[0], []).append(duration)
            if created:
                try:
                    waited = (datetime.fromisoformat(started) - datetime.fromisoformat(created)).total_seconds() / 60
                except ValueError:
                    waited = None
                if waited is not None and waited >= 0:
                    waits.append(waited)
        removed_counts = {"no_show": 0, "removed": 0, "day_end": 0}
        for entry in entries:
            if entry.get("status") != "no_show":
                continue
            if not in_window(entry.get("endedAt") or entry.get("removedAt")):
                continue
            reason = entry.get("removeReason")
            removed_counts[reason if reason in removed_counts else "no_show"] += 1
        services = [
            {
                "name": name,
                "count": count,
                "avgMinutes": (
                    round(sum(service_durations[name]) / len(service_durations[name]))
                    if service_durations.get(name) else None
                ),
            }
            for name, count in sorted(service_counts.items(), key=lambda item: (-item[1], item[0]))
        ]
        return {
            "period": period,
            "label": label,
            "served": len(completed),
            "joined": sum(1 for entry in entries if in_window(entry.get("createdAt"))),
            "noShows": removed_counts["no_show"],
            "removed": removed_counts["removed"],
            "clearedDayEnd": removed_counts["day_end"],
            "avgWaitMinutes": round(sum(waits) / len(waits)) if waits else 0,
            "avgServiceMinutes": round(sum(durations) / len(durations)) if durations else 0,
            "totalServiceMinutes": round(sum(durations)),
            "services": services,
            "_waitTotal": sum(waits),
            "_waitCount": len(waits),
            "_serviceTotal": sum(durations),
            "_serviceCount": len(durations),
        }

    REPORT_METRIC_KEYS = (
        "served", "joined", "noShows", "removed", "clearedDayEnd",
        "avgWaitMinutes", "avgServiceMinutes", "totalServiceMinutes",
    )

    def combined_report_metrics(data_rows):
        wait_total = sum(row["_waitTotal"] for row in data_rows)
        wait_count = sum(row["_waitCount"] for row in data_rows)
        service_total = sum(row["_serviceTotal"] for row in data_rows)
        service_count = sum(row["_serviceCount"] for row in data_rows)
        return {
            "served": sum(row["served"] for row in data_rows),
            "joined": sum(row["joined"] for row in data_rows),
            "noShows": sum(row["noShows"] for row in data_rows),
            "removed": sum(row["removed"] for row in data_rows),
            "clearedDayEnd": sum(row["clearedDayEnd"] for row in data_rows),
            "avgWaitMinutes": round(wait_total / wait_count) if wait_count else 0,
            "avgServiceMinutes": round(service_total / service_count) if service_count else 0,
            "totalServiceMinutes": round(service_total),
        }

    @app.get("/api/providers/<provider_id>/reports")
    def provider_reports(provider_id):
        _, provider = require_owned_provider(provider_id)
        period = request.args.get("period", "day")
        if period not in {"day", "week", "month", "all"}:
            raise ApiError(400, "Choose a day, week, month, or all report.")
        entries = [
            entry for entry in store.list("queues")
            if str(entry.get("providerId")) == str(provider_id)
        ]
        data = provider_report_data(provider, period, entries)
        return jsonify(report={key: value for key, value in data.items() if not key.startswith("_")})

    @app.get("/api/admin/reports")
    def admin_reports():
        require_admin()
        period = request.args.get("period", "day")
        if period not in {"day", "week", "month", "all"}:
            raise ApiError(400, "Choose a day, week, month, or all report.")
        providers = store.list("providers")
        entries_by_provider = {}
        for entry in store.list("queues"):
            entries_by_provider.setdefault(str(entry.get("providerId")), []).append(entry)
        # Each provider is counted in its own timezone, matching its own
        # dashboard report; the shared label uses the default timezone.
        label = provider_report_data({"timezone": DEFAULT_PROVIDER_TIMEZONE}, period, [])["label"]
        data_by_provider = [
            (
                provider,
                provider_report_data(
                    provider, period, entries_by_provider.get(str(provider["id"]), [])
                ),
            )
            for provider in providers
        ]
        by_category = []
        for category in PROVIDER_CATEGORIES:
            rows = [data for provider, data in data_by_provider if provider.get("category") == category]
            if not rows:
                continue
            by_category.append({
                "category": category,
                "providers": len(rows),
                **combined_report_metrics(rows),
            })
        by_category.sort(key=lambda row: (-row["served"], row["category"]))
        by_provider = [
            {
                "providerId": str(provider["id"]),
                "name": provider.get("name", ""),
                "category": provider.get("category", ""),
                "approvalStatus": provider_approval_status(provider),
                **{key: data[key] for key in REPORT_METRIC_KEYS},
            }
            for provider, data in data_by_provider
        ]
        by_provider.sort(key=lambda row: (-row["served"], row["name"].lower()))
        return jsonify(report={
            "period": period,
            "label": label,
            "totals": combined_report_metrics([data for _, data in data_by_provider]),
            "byCategory": by_category,
            "byProvider": by_provider,
        })

    @app.get("/api/queues/current")
    def current_queue():
        user = require_user("customer")
        queues = [
            entry
            for entry in store.list("queues")
            if entry.get("customerId") == user["id"]
            and entry.get("status") in {"waiting", "serving"}
        ]
        queues.sort(key=lambda entry: entry.get("createdAt", ""))
        rolled_over = set()
        enriched = []
        for queue in queues:
            provider = store.get("providers", str(queue["providerId"]))
            if provider is None:
                continue
            if str(provider["id"]) not in rolled_over:
                rolled_over.add(str(provider["id"]))
                with store.transaction():
                    store.lock_providers([str(provider["id"])])
                    fresh = require_provider(str(provider["id"]))
                    if apply_day_rollover(fresh):
                        refresh_provider_metrics(fresh)
                        store.put("providers", str(fresh["id"]), fresh, fresh)
                queue = store.get("queues", queue["id"]) or queue
                if queue.get("status") not in {"waiting", "serving"}:
                    continue
                provider = store.get("providers", str(queue["providerId"])) or provider
            active = active_provider_queues(queue["providerId"])
            position = next((index for index, entry in enumerate(active) if entry["id"] == queue["id"]), None)
            wait = total_wait_minutes(provider, active[:position]) if position is not None else 0
            people_ahead = (
                sum(1 for entry in active[:position] if entry.get("status") == "waiting")
                if position is not None else 0
            )
            enriched.append({
                **queue,
                "buffer": bool(queue.get("buffer")),
                "peopleAhead": people_ahead,
                "wait": wait,
                "position": "Now" if queue.get("status") == "serving" else (position + 1 if position is not None else None),
                "predictedTurnAt": (
                    datetime.now(timezone.utc) + timedelta(minutes=wait)
                ).isoformat() if queue.get("status") == "waiting" else None,
            })
        return jsonify(queues=enriched)

    @app.get("/api/providers/<provider_id>/reviews")
    def list_customer_reviews(provider_id):
        provider = store.get("providers", provider_id)
        if provider is None:
            raise ApiError(404, "Provider not found.")
        reviews = sorted(
            customer_reviews_for(provider["id"]),
            key=lambda review: review.get("createdAt", ""),
            reverse=True,
        )
        return jsonify(
            reviews=[public_customer_review(review) for review in reviews[:20]],
            rating=provider.get("rating", "New"),
            total=len(reviews),
        )

    @app.post("/api/providers/<provider_id>/reviews")
    def create_customer_review(provider_id):
        user = require_user("customer")
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        provider = store.get("providers", provider_id)
        if provider is None:
            raise ApiError(404, "Provider not found.")
        rating = body.get("rating")
        if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
            raise ApiError(400, "Rating must be a whole number of stars from 1 to 5.")
        comment = body.get("comment") or ""
        if not isinstance(comment, str) or len(comment.strip()) > 500:
            raise ApiError(400, "Comment must be 500 characters or fewer.")
        visit_id = body.get("visitId")
        visit = store.get("queues", str(visit_id)) if visit_id else None
        if visit is None:
            raise ApiError(404, "Visit not found.")
        if visit.get("customerId") != user["id"]:
            raise ApiError(403, "You can only review your own visits.")
        if str(visit.get("providerId")) != str(provider["id"]):
            raise ApiError(400, "That visit belongs to a different provider.")
        if visit.get("status") != "completed":
            raise ApiError(409, "Only completed visits can be reviewed.")
        if any(
            str(review.get("visitId")) == str(visit["id"])
            for review in all_customer_reviews()
        ):
            raise ApiError(409, "You have already reviewed this visit.")
        review = {
            "id": str(uuid.uuid4()),
            "providerId": str(provider["id"]),
            "visitId": str(visit["id"]),
            "customerId": user["id"],
            "customerName": user.get("name") or "Customer",
            "rating": rating,
            "comment": comment.strip(),
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
        with store.transaction():
            store.create("customer_reviews", review["id"], review, review)
            fresh = require_provider(str(provider["id"]))
            refresh_provider_rating(fresh)
            store.put("providers", str(fresh["id"]), fresh, fresh)
        log.info(
            "review.create provider=%s visit=%s rating=%s",
            provider["id"], visit["id"], rating,
        )
        return jsonify(
            review=public_customer_review(review),
            rating=fresh["rating"],
            reviews=fresh["reviews"],
        ), 201

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
            store.lock_accounts([user["id"]])
            store.lock_providers([provider_id])
            provider = require_provider(provider_id)
            ensure_provider_approved(provider)
            if provider.get("status") != "Open":
                raise ApiError(409, "This provider is not accepting customers right now.")
            apply_day_rollover(provider)
            # A customer may wait at several providers at once; only a second
            # active visit at the SAME provider is treated as already joined.
            existing = next(
                (
                    entry
                    for entry in store.list("queues")
                    if entry.get("customerId") == user["id"]
                    and str(entry.get("providerId")) == provider_id
                    and entry.get("status") in {"waiting", "serving"}
                ),
                None,
            )
            if existing:
                return jsonify(queue=existing, providers=store.list("providers")), 200

            active_queues = active_provider_queues(provider_id)
            service = resolve_service_selection(provider, body.get("services"))
            wait_minutes = total_wait_minutes(provider, active_queues)
            now_utc = datetime.now(timezone.utc)
            is_buffer = not entry_fits_capacity(
                provider, active_queues, visit_minutes(provider, service)
            )
            queue = {
                "id": str(uuid.uuid4()),
                "customerId": user["id"],
                "providerId": provider["id"],
                "service": service,
                "peopleAhead": len(active_queues),
                "wait": wait_minutes,
                "status": "waiting",
                "buffer": is_buffer,
                "predictedWaitMinutes": wait_minutes,
                "predictedTurnAt": (now_utc + timedelta(minutes=wait_minutes)).isoformat(),
                "createdAt": now_utc.isoformat(),
            }
            if not store.create("queues", queue["id"], queue, queue):
                log.warning("queue.join_conflict customer=%s provider=%s", user["id"], provider["id"])
                raise ApiError(409, "We couldn't add you to this queue. Refresh and try again.")
            log.info(
                "queue.join queue=%s customer=%s provider=%s service=%s",
                queue["id"],
                user["id"],
                provider["id"],
                queue["service"],
            )
            log.info(
                "queue.prediction queue=%s provider=%s predicted_wait=%s buffer=%s",
                queue["id"],
                provider["id"],
                wait_minutes,
                is_buffer,
            )
            refresh_provider_metrics(provider)
            store.put("providers", str(provider["id"]), provider, provider)
            return jsonify(queue=queue, providers=store.list("providers")), 201

    @app.post("/api/queues/<queue_id>/dismiss-review")
    def dismiss_review_prompt(queue_id):
        user = require_user("customer")
        if not CLIENT_ID.fullmatch(queue_id):
            raise ApiError(400, "A valid queue ID is required.")
        visit = store.get("queues", queue_id)
        if not visit or visit.get("customerId") != user["id"]:
            raise ApiError(404, "Visit not found.")
        visit["reviewDismissed"] = True
        store.put("queues", visit["id"], visit, visit)
        return jsonify(ok=True)

    @app.delete("/api/queues/<queue_id>")
    def leave_queue(queue_id):
        user = require_user("customer")
        if not CLIENT_ID.fullmatch(queue_id):
            raise ApiError(400, "A valid queue ID is required.")
        with store.transaction():
            store.lock_accounts([user["id"]])
            queue = store.get("queues", queue_id)
            if (
                not queue
                or queue.get("customerId") != user["id"]
                or queue.get("status") != "waiting"
            ):
                raise ApiError(404, "Active queue entry not found.")
            store.lock_providers([queue["providerId"]])
            queue = store.get("queues", queue_id)
            if (
                not queue
                or queue.get("customerId") != user["id"]
                or queue.get("status") != "waiting"
            ):
                raise ApiError(404, "Active queue entry not found.")
            provider = require_provider(str(queue["providerId"]))
            store.delete("queues", queue_id)
            log.info(
                "queue.leave queue=%s customer=%s provider=%s",
                queue_id,
                user["id"],
                queue["providerId"],
            )
            promote_buffer_entries(provider)
            refresh_provider_metrics(provider)
            store.put("providers", str(provider["id"]), provider, provider)
            return jsonify(providers=store.list("providers"))

    @app.post("/api/providers/<provider_id>/call-next")
    def call_next(provider_id):
        with store.transaction():
            store.lock_providers([provider_id])
            _, provider = require_owned_provider(provider_id)
            ensure_provider_approved(provider)
            active = active_provider_queues(provider_id)
            if sum(1 for queue in active if queue.get("status") == "serving") >= provider_staff_limit(provider):
                raise ApiError(409, "All your staff are busy. Complete a service before starting another.")
            waiting = [entry for entry in active if entry.get("status") == "waiting"]
            if not waiting:
                raise ApiError(409, "There are no waiting customers.")
            entry = waiting[0]
            entry["status"] = "serving"
            entry["startedAt"] = datetime.now(timezone.utc).isoformat()
            record_prediction_outcome(entry)
            store.put("queues", entry["id"], entry, entry)
            log.info("queue.call_next queue=%s provider=%s", entry["id"], provider["id"])
            refresh_provider_metrics(provider)
            store.put("providers", str(provider["id"]), provider, provider)
            return jsonify(queue=entry, providers=store.list("providers"))

    @app.post("/api/providers/<provider_id>/walk-ins")
    def add_walk_in(provider_id):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        with store.transaction():
            _, provider = require_owned_provider(provider_id)
            entry = create_walk_in_entry(provider, body)
            return jsonify(queue=entry, providers=store.list("providers")), 201

    @app.patch("/api/admin/providers/<provider_id>/approval")
    def update_provider_approval(provider_id):
        require_admin()
        body = request.get_json(silent=True)
        if (
            not isinstance(body, dict)
            or body.get("action") not in {"approve", "reject"}
            or set(body) - {"action", "note"}
        ):
            raise ApiError(400, "Choose approve or reject and provide an optional review note.")
        note = body.get("note", "")
        if not isinstance(note, str) or len(note.strip()) > 500:
            raise ApiError(400, "Review notes must be 500 characters or fewer.")
        with store.transaction():
            store.lock_providers([provider_id])
            provider = require_provider(provider_id)
            provider["approvalStatus"] = "approved" if body["action"] == "approve" else "rejected"
            provider["approvalNote"] = note.strip()
            provider["approvalUpdatedAt"] = datetime.now(timezone.utc).isoformat()
            if body["action"] == "reject":
                provider["status"] = "Paused"
            store.put("providers", str(provider["id"]), provider, provider)
            event = {
                "id": str(uuid.uuid4()),
                "providerId": provider["id"],
                "decision": provider["approvalStatus"],
                "note": provider["approvalNote"],
                "createdAt": provider["approvalUpdatedAt"],
            }
            store.create("provider_reviews", event["id"], event, event)
            log.info("provider.approval provider=%s decision=%s", provider["id"], provider["approvalStatus"])
            return jsonify(provider=provider)

    @app.post("/api/providers/<provider_id>/request-review")
    def request_provider_review(provider_id):
        with store.transaction():
            store.lock_providers([provider_id])
            _, provider = require_owned_provider(provider_id)
            if provider_approval_status(provider) != "rejected":
                raise ApiError(409, "Only rejected provider listings can request another review.")
            provider["approvalStatus"] = "pending"
            provider["approvalUpdatedAt"] = datetime.now(timezone.utc).isoformat()
            store.put("providers", str(provider["id"]), provider, provider)
            event = {
                "id": str(uuid.uuid4()),
                "providerId": provider["id"],
                "decision": "resubmitted",
                "note": provider.get("approvalNote", ""),
                "createdAt": provider["approvalUpdatedAt"],
            }
            store.create("provider_reviews", event["id"], event, event)
            return jsonify(provider=provider)

    @app.post("/api/providers/<provider_id>/photo")
    def update_provider_photo(provider_id):
        image_contents, image_error = process_photo(request.files.get("photo"))
        if image_error:
            raise ApiError(400, image_error)
        if not image_contents:
            raise ApiError(400, "Choose a JPEG, PNG, or WebP listing photo.")
        _, provider = require_owned_provider(provider_id)
        photo_id = f"{uuid.uuid4().hex}.jpg"
        save_provider_photo(provider_id, photo_id, image_contents)
        previous_photo_id = None
        with store.transaction():
            store.lock_providers([provider_id])
            _, provider = require_owned_provider(provider_id)
            previous_photo_id = provider.get("photoId")
            provider["photoId"] = photo_id
            store.put("providers", str(provider["id"]), provider, provider)
        if isinstance(previous_photo_id, str) and previous_photo_id != photo_id and re.fullmatch(r"[a-f0-9]{32}\.jpg", previous_photo_id):
            delete_provider_photo(previous_photo_id)
        return jsonify(provider=provider)

    @app.patch("/api/providers/<provider_id>")
    def update_provider(provider_id):
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        allowed = {
            "status", "name", "category", "address", "description", "services", "hours",
            "serviceDuration", "serviceDurations", "contactEmail", "contactPhone",
            "openTime", "closeTime", "timezone", "bufferMinutes", "carryForwardQueue", "staffCount",
            "privateListing",
        }
        if not body or set(body) - allowed:
            raise ApiError(400, "Only provider profile and queue settings can be changed.")
        with store.transaction():
            store.lock_providers([provider_id])
            _, provider = require_owned_provider(provider_id)
            if "status" in body:
                if not isinstance(body["status"], str) or body["status"] not in {"Open", "Paused"}:
                    raise ApiError(400, "Status must be Open or Paused.")
                if body["status"] == "Open":
                    ensure_provider_approved(provider)
                provider["status"] = body["status"]
            for key, limit in (("name", 120), ("address", 200), ("hours", 100)):
                if key in body:
                    if not isinstance(body[key], str) or not body[key].strip() or len(body[key].strip()) > limit:
                        raise ApiError(400, f"{key} must contain 1 to {limit} characters.")
                    provider[key] = body[key].strip()
            if "description" in body:
                description = body["description"]
                if not isinstance(description, str) or len(description.strip()) > 160:
                    raise ApiError(400, "Tagline must be no more than 160 characters.")
                provider["description"] = description.strip()
            if "category" in body:
                if not isinstance(body["category"], str) or body["category"] not in PROVIDER_CATEGORIES:
                    raise ApiError(400, "Choose a valid business category.")
                provider["category"] = body["category"]
                provider["accent"], provider["icon"] = PROVIDER_CATEGORIES[body["category"]]
            if "contactEmail" in body:
                contact_email = body["contactEmail"]
                if not isinstance(contact_email, str):
                    raise ApiError(400, "Business email must be a valid email address.")
                contact_email = contact_email.strip()
                if contact_email and not normalized_email(contact_email):
                    raise ApiError(400, "Business email must be a valid email address.")
                provider["contactEmail"] = contact_email.casefold() or None
            if "contactPhone" in body:
                contact_phone = body["contactPhone"]
                if not isinstance(contact_phone, str):
                    raise ApiError(400, "Business phone must be a valid international phone number.")
                contact_phone = contact_phone.strip()
                if contact_phone and not normalized_phone(contact_phone):
                    raise ApiError(400, "Business phone must be a valid international phone number.")
                provider["contactPhone"] = normalized_phone(contact_phone) if contact_phone else None
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
            if "serviceDurations" in body:
                durations = body["serviceDurations"]
                if not isinstance(durations, dict) or len(durations) > 20:
                    raise ApiError(400, "Service times must be an object mapping service names to minutes.")
                cleaned = {}
                for name, minutes in durations.items():
                    if (
                        not isinstance(name, str)
                        or not name.strip()
                        or len(name.strip()) > 60
                        or not isinstance(minutes, int)
                        or isinstance(minutes, bool)
                        or not 1 <= minutes <= 180
                    ):
                        raise ApiError(400, "Each service time must be between 1 and 180 minutes.")
                    cleaned[name.strip()] = minutes
                provider["serviceDurations"] = cleaned
            for key in ("openTime", "closeTime"):
                if key in body:
                    value = body[key]
                    if value is None or value == "":
                        provider.pop(key, None)
                    elif parse_hhmm(value) is None:
                        raise ApiError(400, "Opening and closing times must be in 24-hour HH:MM format.")
                    else:
                        provider[key] = value.strip()
            if ("openTime" in body or "closeTime" in body) or provider.get("openTime") or provider.get("closeTime"):
                opening = parse_hhmm(provider.get("openTime"))
                closing = parse_hhmm(provider.get("closeTime"))
                if (opening is None) != (closing is None):
                    raise ApiError(400, "Set both opening and closing times, or clear both.")
                if opening is not None and closing <= opening:
                    raise ApiError(400, "Closing time must be after opening time.")
            if "timezone" in body:
                if not isinstance(body["timezone"], str) or body["timezone"] not in PROVIDER_TIMEZONES:
                    raise ApiError(400, "Choose a valid timezone.")
                provider["timezone"] = body["timezone"]
            if "bufferMinutes" in body:
                buffer_minutes = body["bufferMinutes"]
                if not isinstance(buffer_minutes, int) or isinstance(buffer_minutes, bool) or not 0 <= buffer_minutes <= 240:
                    raise ApiError(400, "Buffer minutes must be between 0 and 240.")
                provider["bufferMinutes"] = buffer_minutes
            if "staffCount" in body:
                staff_count = body["staffCount"]
                if not isinstance(staff_count, int) or isinstance(staff_count, bool) or not 1 <= staff_count <= 20:
                    raise ApiError(400, "Staff count must be a whole number between 1 and 20.")
                provider["staffCount"] = staff_count
            if "carryForwardQueue" in body:
                if not isinstance(body["carryForwardQueue"], bool):
                    raise ApiError(400, "Carry-forward must be true or false.")
                provider["carryForwardQueue"] = body["carryForwardQueue"]
            if "privateListing" in body:
                if not isinstance(body["privateListing"], bool):
                    raise ApiError(400, "Private listing must be true or false.")
                provider["privateListing"] = body["privateListing"]
            if "services" in body or "serviceDuration" in body or "serviceDurations" in body:
                # Keep per-service times in step with the final service list:
                # drop times for services that no longer exist, and drop times
                # that just repeat the average (the fallback covers those).
                average = max(1, int(provider.get("serviceDuration", 15)))
                kept = {
                    name: minutes
                    for name, minutes in provider_service_minutes(provider).items()
                    if name in (provider.get("services") or []) and minutes != average
                }
                if kept:
                    provider["serviceDurations"] = kept
                else:
                    provider.pop("serviceDurations", None)
                refresh_provider_metrics(provider)
            store.put("providers", str(provider["id"]), provider, provider)
            log.info("provider.update provider=%s fields=%s", provider["id"], ",".join(sorted(body)))
            return jsonify(providers=store.list("providers"))

    @app.patch("/api/providers/<provider_id>/queues/<queue_id>")
    def update_provider_queue(provider_id, queue_id):
        body = request.get_json(silent=True)
        if (
            not isinstance(body, dict)
            or not isinstance(body.get("action"), str)
            or body["action"] not in {"serve", "complete", "remove", "restore"}
            or set(body) - {"action", "reason"}
        ):
            raise ApiError(400, "Choose serve, complete, remove, or restore.")
        if not CLIENT_ID.fullmatch(queue_id):
            raise ApiError(400, "A valid queue ID is required.")
        with store.transaction():
            store.lock_providers([provider_id])
            _, provider = require_owned_provider(provider_id)
            apply_day_rollover(provider)
            queue = store.get("queues", queue_id)
            if not queue or str(queue.get("providerId")) != provider_id:
                raise ApiError(404, "Queue entry not found.")
            if body["action"] == "serve":
                ensure_provider_approved(provider)
                if queue.get("status") != "waiting":
                    raise ApiError(409, "Only waiting customers can be started.")
                if sum(1 for entry in active_provider_queues(provider_id) if entry.get("status") == "serving") >= provider_staff_limit(provider):
                    raise ApiError(409, "All your staff are busy. Complete a service before starting another.")
                queue["status"] = "serving"
                queue["startedAt"] = datetime.now(timezone.utc).isoformat()
                record_prediction_outcome(queue)
            elif body["action"] == "complete":
                if queue.get("status") != "serving":
                    raise ApiError(409, "Only a customer currently being served can be completed.")
                queue["status"] = "completed"
                queue["completedAt"] = datetime.now(timezone.utc).isoformat()
                record_service_time(provider, queue)
            elif body["action"] == "remove":
                if queue.get("status") != "waiting":
                    raise ApiError(409, "Only waiting customers can be removed from the queue.")
                reason = body.get("reason")
                if reason not in {"no_show", "removed"}:
                    reason = "no_show"
                queue["status"] = "no_show"
                queue["buffer"] = False
                queue["removedAt"] = datetime.now(timezone.utc).isoformat()
                queue["endedAt"] = queue["removedAt"]
                queue["removeReason"] = reason
                log.info(
                    "queue.remove queue=%s provider=%s reason=%s",
                    queue["id"],
                    provider["id"],
                    reason,
                )
            else:  # restore
                if queue.get("status") != "no_show":
                    raise ApiError(409, "Only dequeued customers can be brought back.")
                customer_id = queue.get("customerId")
                if customer_id and any(
                    entry.get("customerId") == customer_id and entry["id"] != queue["id"]
                    for entry in active_provider_queues(provider_id)
                ):
                    raise ApiError(409, "This customer is already in the queue.")
                now_utc = datetime.now(timezone.utc)
                queue["status"] = "waiting"
                queue["createdAt"] = now_utc.isoformat()
                for field in ("startedAt", "completedAt", "endedAt", "removedAt", "removeReason", "carriedOver"):
                    queue.pop(field, None)
                active = active_provider_queues(provider_id)
                wait_minutes = total_wait_minutes(provider, active)
                queue["peopleAhead"] = len(active)
                queue["wait"] = wait_minutes
                queue["predictedWaitMinutes"] = wait_minutes
                queue["predictedTurnAt"] = (now_utc + timedelta(minutes=wait_minutes)).isoformat()
                queue["buffer"] = not entry_fits_capacity(
                    provider, active, visit_minutes(provider, queue.get("service"))
                )
                log.info("queue.restore queue=%s provider=%s", queue["id"], provider["id"])
            store.put("queues", queue["id"], queue, queue)
            if body["action"] in {"complete", "remove"}:
                promote_buffer_entries(provider)
            if body["action"] != "remove":
                log.info(
                    "queue.%s queue=%s provider=%s",
                    body["action"],
                    queue["id"],
                    provider["id"],
                )
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
