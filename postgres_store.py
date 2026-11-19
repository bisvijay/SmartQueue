import json
import logging
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlsplit
from uuid import UUID, uuid4, uuid5, NAMESPACE_URL

import pg8000.dbapi


logger = logging.getLogger("smartqueue.postgres")

TABLES = {
    "users": "accounts",
    "providers": "providers",
    "queues": "queue_visits",
    "provider_reviews": "provider_reviews",
}


class _Result:
    def __init__(self, cursor):
        self.cursor = cursor
        self.columns = [column[0] for column in cursor.description] if cursor.description else []

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(zip(self.columns, row)) if row is not None else None

    def fetchall(self):
        return [dict(zip(self.columns, row)) for row in self.cursor.fetchall()]


class _Connection:
    def __init__(self, raw_connection):
        self.raw = raw_connection

    def execute(self, query, params=None):
        cursor = self.raw.cursor()
        cursor.execute(query, params or ())
        return _Result(cursor)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def _uuid(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return str(uuid5(NAMESPACE_URL, f"smartqueue:{value}"))


def _timestamp(value):
    if not value:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


class PostgresStore:
    """PostgreSQL adapter for the app's existing record-store interface.

    Connections are reused per thread instead of opened fresh for every
    operation. Against a remote database (e.g. the app in the US talking to
    Supabase in ap-south-1) each new connection costs a TCP + TLS + login
    handshake of several round trips, so listing N providers used to cost
    N+1 handshakes. One persistent connection per thread turns that into
    one handshake, plus a cheap liveness probe if the connection has sat
    idle long enough that the pooler may have closed it.
    """

    # Probe a cached connection that has been idle longer than this, so a
    # connection the pooler closed while idle is replaced before use
    # instead of failing the first request after the idle period.
    _IDLE_PROBE_SECONDS = 30.0

    def __init__(self, dsn, ssl_root_cert=None):
        if not isinstance(dsn, str) or not dsn.startswith(("postgres://", "postgresql://")):
            raise ValueError("DATABASE_URL must be a PostgreSQL connection URI.")
        self.dsn = dsn
        self.ssl_root_cert = ssl_root_cert
        self._local = threading.local()
        self._ssl_contexts = {}

    @staticmethod
    def _create_ssl_context(sslmode, ssl_root_cert):
        if sslmode in {"disable", "allow"}:
            raise ValueError("PostgreSQL SSL must remain enabled for SmartQueue connections.")
        return ssl.create_default_context(cafile=ssl_root_cert or None)

    def _get_ssl_context(self, sslmode):
        key = (sslmode, self.ssl_root_cert)
        ssl_context = self._ssl_contexts.get(key)
        if ssl_context is None:
            ssl_context = self._create_ssl_context(sslmode, self.ssl_root_cert)
            self._ssl_contexts[key] = ssl_context
        return ssl_context

    def _open_connection(self):
        dsn = self.dsn
        parsed = urlsplit(dsn)
        if not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError("DATABASE_URL must include a PostgreSQL host and database name.")
        query = parse_qs(parsed.query)
        sslmode = query.get("sslmode", ["require"])[0]
        raw_connection = pg8000.dbapi.connect(
            host=parsed.hostname,
            port=parsed.port or 5432,
            user=unquote(parsed.username or ""),
            password=unquote(parsed.password or ""),
            database=unquote(parsed.path.lstrip("/")),
            ssl_context=self._get_ssl_context(sslmode),
        )
        return _Connection(raw_connection)

    @staticmethod
    def _is_closed(connection):
        # pg8000 sets the underlying socket to None when a connection is
        # closed. Anything without that attribute is not a pg8000
        # connection, so assume it is usable rather than discarding it.
        raw = getattr(connection, "raw", None)
        return raw is not None and hasattr(raw, "_usock") and raw._usock is None

    def _discard_persistent_connection(self, quiet=False):
        connection = getattr(self._local, "persistent_connection", None)
        self._local.persistent_connection = None
        self._local.persistent_last_used = None
        if connection is not None:
            if not quiet:
                logger.warning("postgres connection discarded; the next operation reconnects")
            try:
                connection.close()
            except Exception:
                pass

    def _get_persistent_connection(self):
        connection = getattr(self._local, "persistent_connection", None)
        last_used = getattr(self._local, "persistent_last_used", None)
        if connection is not None and self._is_closed(connection):
            self._discard_persistent_connection()
            connection = None
        if connection is not None and last_used is not None:
            idle = time.monotonic() - last_used
            if idle > self._IDLE_PROBE_SECONDS:
                try:
                    connection.execute("SELECT 1").fetchone()
                except Exception:
                    self._discard_persistent_connection()
                    connection = None
        if connection is None:
            connection = self._open_connection()
            logger.debug("postgres connection established")
            self._local.persistent_connection = connection
        self._local.persistent_last_used = time.monotonic()
        return connection

    def close(self):
        """Close this thread's persistent connection, if any.

        The next operation opens a fresh one. Mainly for scripts and
        tests; the long-running app simply keeps reusing its connection.
        """
        self._discard_persistent_connection(quiet=True)

    @staticmethod
    def _is_connection_error(error):
        return isinstance(
            error, (pg8000.dbapi.InterfaceError, pg8000.dbapi.OperationalError)
        )

    @contextmanager
    def transaction(self):
        if getattr(self._local, "connection", None) is not None:
            savepoint = f"smartqueue_sp_{uuid4().hex}"
            self._local.connection.execute(f"SAVEPOINT {savepoint}")
            try:
                yield
                self._local.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            except Exception:
                self._local.connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                self._local.connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                raise
            return
        connection = self._get_persistent_connection()
        self._local.connection = connection
        try:
            yield
            connection.commit()
        except Exception as error:
            try:
                connection.rollback()
            except Exception:
                pass
            if self._is_connection_error(error):
                self._discard_persistent_connection()
            raise
        finally:
            self._local.connection = None
            self._local.persistent_last_used = time.monotonic()
            # The connection stays open for reuse; only a broken one was
            # discarded above, so the next operation reconnects instead of
            # failing on a dead socket.

    @contextmanager
    def _connection(self):
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            yield connection
            return
        connection = self._get_persistent_connection()
        try:
            yield connection
            connection.commit()
        except Exception as error:
            try:
                connection.rollback()
            except Exception:
                pass
            if self._is_connection_error(error):
                self._discard_persistent_connection()
            raise
        finally:
            self._local.persistent_last_used = time.monotonic()

    @staticmethod
    def _extra(record, keys):
        return {key: value for key, value in record.items() if key not in keys}

    def _user_record(self, row, connection):
        record = dict(row["app_data"] or {})
        record.update({
            "id": str(row["id"]),
            "name": row["name"],
            "role": row["role"],
            "email": row["email"],
            "phone": row["phone_e164"],
            "createdAt": row["created_at"].isoformat(),
            "passwordHash": row["password_hash"],
        })
        if row["updated_at"]:
            record["updatedAt"] = row["updated_at"].isoformat()
        favorites = connection.execute(
            "SELECT provider_id FROM provider_favorites WHERE customer_account_id = %s ORDER BY created_at",
            (row["id"],),
        ).fetchall()
        if favorites:
            record["favoriteProviderIds"] = [str(item["provider_id"]) for item in favorites]
        else:
            record.pop("favoriteProviderIds", None)
        return record

    def _provider_record(self, row, connection):
        record = dict(row["app_data"] or {})
        services = connection.execute(
            "SELECT name FROM provider_services WHERE provider_id = %s AND is_active ORDER BY display_order, name",
            (row["id"],),
        ).fetchall()
        record.update({
            "id": str(row["id"]),
            "ownerId": str(row["owner_account_id"]) if row["owner_account_id"] else None,
            "name": row["name"],
            "category": row["category"],
            "description": row["tagline"],
            "address": row["address"],
            "status": "Open" if row["status"] == "open" else "Paused",
            "services": [service["name"] for service in services],
            "contactEmail": row["public_email"],
            "contactPhone": row["public_phone_e164"],
            "approvalStatus": row["approval_status"],
            "approvalNote": row["approval_note"],
        })
        if row["approval_updated_at"]:
            record["approvalUpdatedAt"] = row["approval_updated_at"].isoformat()
        return record

    @staticmethod
    def _queue_record(row):
        record = dict(row["app_data"] or {})
        record.update({
            "id": str(row["id"]),
            "providerId": str(row["provider_id"]),
            "customerId": str(row["customer_account_id"]) if row["customer_account_id"] else None,
            "customerName": row["customer_name"],
            "email": row["customer_email"],
            "phone": row["customer_phone_e164"],
            "source": row["source"],
            "status": row["status"],
            "createdAt": row["joined_at"].isoformat(),
        })
        if row["service_name"]:
            record["service"] = row["service_name"]
        for key, value in (
            ("startedAt", row["started_at"]),
            ("completedAt", row["completed_at"]),
            ("endedAt", row["ended_at"]),
        ):
            if value:
                record[key] = value.isoformat()
        return record

    def _get(self, connection, collection, record_id):
        if collection == "users":
            row = connection.execute("SELECT * FROM accounts WHERE id = %s", (_uuid(record_id),)).fetchone()
            return self._user_record(row, connection) if row else None
        if collection == "providers":
            row = connection.execute("SELECT * FROM providers WHERE id = %s", (_uuid(record_id),)).fetchone()
            return self._provider_record(row, connection) if row else None
        if collection == "queues":
            row = connection.execute("SELECT * FROM queue_visits WHERE id = %s", (_uuid(record_id),)).fetchone()
            return self._queue_record(row) if row else None
        if collection == "provider_reviews":
            row = connection.execute("SELECT * FROM provider_reviews WHERE id = %s", (_uuid(record_id),)).fetchone()
            if not row:
                return None
            return {
                **dict(row["app_data"] or {}),
                "id": str(row["id"]),
                "providerId": str(row["provider_id"]),
                "decision": row["decision"],
                "note": row["note"],
                "createdAt": row["created_at"].isoformat(),
            }
        raise ValueError(f"Unknown collection: {collection}")

    def get(self, collection, record_id):
        with self._connection() as connection:
            return self._get(connection, collection, record_id)

    def lock_accounts(self, account_ids):
        with self._connection() as connection:
            for account_id in sorted({_uuid(value) for value in account_ids}):
                connection.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account_id,)).fetchone()

    def lock_providers(self, provider_ids):
        with self._connection() as connection:
            for provider_id in sorted({_uuid(value) for value in provider_ids}):
                connection.execute("SELECT id FROM providers WHERE id = %s FOR UPDATE", (provider_id,)).fetchone()

    def list(self, collection):
        table = TABLES.get(collection)
        if not table:
            raise ValueError(f"Unknown collection: {collection}")
        with self._connection() as connection:
            if collection == "users":
                rows = connection.execute("SELECT * FROM accounts WHERE deactivated_at IS NULL ORDER BY created_at, id").fetchall()
                records = [self._user_record(row, connection) for row in rows]
                return [{key: value for key, value in record.items() if key != "passwordHash"} for record in records]
            if collection == "providers":
                rows = connection.execute("SELECT * FROM providers ORDER BY created_at, id").fetchall()
                return [self._provider_record(row, connection) for row in rows]
            if collection == "queues":
                rows = connection.execute("SELECT * FROM queue_visits ORDER BY joined_at, id").fetchall()
                return [self._queue_record(row) for row in rows]
            rows = connection.execute("SELECT * FROM provider_reviews ORDER BY created_at, id").fetchall()
            return [
                {**dict(row["app_data"] or {}), "id": str(row["id"]), "providerId": str(row["provider_id"]),
                 "decision": row["decision"], "note": row["note"], "createdAt": row["created_at"].isoformat()}
                for row in rows
            ]

    def _write_user(self, connection, record, create=False):
        record_id = _uuid(record["id"])
        extra = self._extra(record, {
            "id", "name", "role", "email", "phone", "passwordHash", "createdAt", "updatedAt",
            "providerId", "favoriteProviderIds",
        })
        if record.get("providerId"):
            extra["providerId"] = _uuid(record["providerId"])
        values = (
            record_id, record["role"], record["name"], record.get("email"), record.get("phone"),
            record.get("passwordHash", ""), json.dumps(extra), _timestamp(record.get("createdAt")),
            _timestamp(record.get("updatedAt")),
        )
        command = """
            INSERT INTO accounts (id, role, name, email, phone_e164, password_hash, app_data, created_at, updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        """
        if create:
            cursor = connection.execute(command + " ON CONFLICT DO NOTHING RETURNING id", values)
            created = cursor.fetchone() is not None
        else:
            connection.execute(command + """
                ON CONFLICT (id) DO UPDATE SET role=EXCLUDED.role, name=EXCLUDED.name,
                email=EXCLUDED.email, phone_e164=EXCLUDED.phone_e164,
                password_hash=EXCLUDED.password_hash, app_data=EXCLUDED.app_data,
                updated_at=EXCLUDED.updated_at
            """, values)
            created = True
        if not created:
            return False
        account_id = UUID(record_id)
        connection.execute("DELETE FROM provider_favorites WHERE customer_account_id = %s", (account_id,))
        for provider_id in record.get("favoriteProviderIds", []):
            connection.execute(
                "INSERT INTO provider_favorites (customer_account_id, provider_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (account_id, _uuid(provider_id)),
            )
        return True

    def _write_provider(self, connection, record, create=False):
        record_id = _uuid(record["id"])
        owner_id = _uuid(record["ownerId"]) if record.get("ownerId") else None
        status = "open" if record.get("status", "Open") == "Open" else "paused"
        extra = self._extra(record, {
            "id", "ownerId", "name", "category", "description", "address", "status", "services",
            "contactEmail", "contactPhone", "approvalStatus", "approvalNote", "approvalUpdatedAt",
        })
        values = (
            record_id, owner_id, record["name"], record["category"], record.get("description", ""),
            record["address"], record.get("contactEmail"), record.get("contactPhone"), status,
            record.get("approvalStatus", "approved"), record.get("approvalNote", ""),
            _timestamp(record["approvalUpdatedAt"]) if record.get("approvalUpdatedAt") else None,
            json.dumps(extra),
        )
        command = """
            INSERT INTO providers
                (id, owner_account_id, name, category, tagline, address, public_email,
                 public_phone_e164, status, approval_status, approval_note, approval_updated_at, app_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """
        if create:
            created = connection.execute(command + " ON CONFLICT DO NOTHING RETURNING id", values).fetchone() is not None
        else:
            connection.execute(command + """
                ON CONFLICT (id) DO UPDATE SET owner_account_id=EXCLUDED.owner_account_id,
                name=EXCLUDED.name, category=EXCLUDED.category, tagline=EXCLUDED.tagline,
                address=EXCLUDED.address, public_email=EXCLUDED.public_email,
                public_phone_e164=EXCLUDED.public_phone_e164, status=EXCLUDED.status,
                approval_status=EXCLUDED.approval_status, approval_note=EXCLUDED.approval_note,
                approval_updated_at=EXCLUDED.approval_updated_at, app_data=EXCLUDED.app_data,
                updated_at=now()
            """, values)
            created = True
        if not created:
            return False
        provider_id = UUID(record_id)
        service_names = record.get("services", [])
        connection.execute("DELETE FROM provider_services WHERE provider_id = %s", (provider_id,))
        for order, name in enumerate(service_names):
            connection.execute("""
                INSERT INTO provider_services (id, provider_id, name, estimated_minutes, display_order)
                VALUES (%s, %s, %s, %s, %s)
            """, (str(uuid4()), provider_id, name, max(1, int(record.get("serviceDuration", 15))), order))
        return True

    def _write_queue(self, connection, record, create=False):
        record_id = _uuid(record["id"])
        source = record.get("source", "customer_app")
        source = {"walk-in": "provider_dashboard", "qr": "public_qr"}.get(source, source)
        customer_id = _uuid(record["customerId"]) if record.get("customerId") else None
        service = record.get("service") or record.get("service_name") or ""
        extra = self._extra(record, {
            "id", "providerId", "customerId", "customerName", "email", "phone", "service",
            "source", "status", "createdAt", "startedAt", "completedAt", "endedAt",
        })
        values = (
            record_id, _uuid(record["providerId"]), customer_id, record.get("customerName", "Customer"),
            record.get("email"), record.get("phone"), service, source, record.get("status", "waiting"),
            _timestamp(record.get("createdAt")), _timestamp(record["startedAt"]) if record.get("startedAt") else None,
            _timestamp(record["completedAt"]) if record.get("completedAt") else None,
            _timestamp(record["endedAt"]) if record.get("endedAt") else None, json.dumps(extra),
        )
        command = """
            INSERT INTO queue_visits
                (id, provider_id, customer_account_id, customer_name, customer_email, customer_phone_e164,
                 service_name, source, status, joined_at, started_at, completed_at, ended_at, app_data)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """
        if create:
            return connection.execute(command + " ON CONFLICT DO NOTHING RETURNING id", values).fetchone() is not None
        connection.execute(command + """
            ON CONFLICT (id) DO UPDATE SET provider_id=EXCLUDED.provider_id,
            customer_account_id=EXCLUDED.customer_account_id, customer_name=EXCLUDED.customer_name,
            customer_email=EXCLUDED.customer_email, customer_phone_e164=EXCLUDED.customer_phone_e164,
            service_name=EXCLUDED.service_name, source=EXCLUDED.source, status=EXCLUDED.status,
            joined_at=EXCLUDED.joined_at, started_at=EXCLUDED.started_at,
            completed_at=EXCLUDED.completed_at, ended_at=EXCLUDED.ended_at, app_data=EXCLUDED.app_data
        """, values)
        return True

    def _write_review(self, connection, record, create=False):
        values = (
            _uuid(record["id"]), _uuid(record["providerId"]), record["decision"],
            record.get("note", ""), _timestamp(record.get("createdAt")), "{}",
        )
        command = """
            INSERT INTO provider_reviews (id, provider_id, decision, note, created_at, app_data)
            VALUES (%s, %s, %s, %s, %s, %s::jsonb)
        """
        if create:
            return connection.execute(command + " ON CONFLICT DO NOTHING RETURNING id", values).fetchone() is not None
        connection.execute(command + """
            ON CONFLICT (id) DO UPDATE SET provider_id=EXCLUDED.provider_id,
            decision=EXCLUDED.decision, note=EXCLUDED.note, created_at=EXCLUDED.created_at
        """, values)
        return True

    def put(self, collection, record_id, data, index_entry=None):
        record = dict(data)
        record["id"] = str(record_id)
        writer = {
            "users": self._write_user,
            "providers": self._write_provider,
            "queues": self._write_queue,
            "provider_reviews": self._write_review,
        }.get(collection)
        if not writer:
            raise ValueError(f"Unknown collection: {collection}")
        with self._connection() as connection:
            writer(connection, record)

    def create(self, collection, record_id, data, index_entry=None):
        record = dict(data)
        record["id"] = str(record_id)
        writer = {
            "users": self._write_user,
            "providers": self._write_provider,
            "queues": self._write_queue,
            "provider_reviews": self._write_review,
        }.get(collection)
        if not writer:
            raise ValueError(f"Unknown collection: {collection}")
        with self._connection() as connection:
            return writer(connection, record, create=True)

    def delete(self, collection, record_id):
        table = TABLES.get(collection)
        if not table:
            raise ValueError(f"Unknown collection: {collection}")
        with self._connection() as connection:
            connection.execute(f"DELETE FROM {table} WHERE id = %s", (_uuid(record_id),))

    def export_all(self, collection):
        return [self.get(collection, item["id"]) for item in self.list(collection)]

    def get_config(self, relative_path):
        raise NotImplementedError("File-based configuration is not stored in PostgreSQL.")

    def put_config(self, relative_path, data):
        raise NotImplementedError("File-based configuration is not stored in PostgreSQL.")
