import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from postgres_store import PostgresStore, _uuid
from store import FileStore


MIGRATION = ROOT / "migrations" / "001_postgres_app_compat.sql"


def collection_count(connection, table):
    return connection.execute(f"SELECT count(*) AS total FROM {table}").fetchone()["total"]


def ensure_empty_target(store):
    with store.transaction():
        connection = store._local.connection
        counts = {
            "accounts": collection_count(connection, "accounts"),
            "providers": collection_count(connection, "providers"),
            "queue_visits": collection_count(connection, "queue_visits"),
            "provider_reviews": collection_count(connection, "provider_reviews"),
        }
    if any(counts.values()):
        raise RuntimeError(
            "Import stopped: target tables are not empty. No data was changed. "
            "Use a clean project or plan a separate merge."
        )


def import_json(store, source_dir):
    source = FileStore(source_dir)
    users = source.export_all("users")
    providers = source.export_all("providers")
    queues = source.export_all("queues")
    reviews = source.export_all("provider_reviews")

    account_map = {str(user["id"]): _uuid(user["id"]) for user in users}
    provider_map = {str(provider["id"]): _uuid(provider["id"]) for provider in providers}
    with store.transaction():
        for user in users:
            user["id"] = account_map[str(user["id"])]
            user["providerId"] = provider_map.get(str(user.get("providerId")))
            user["favoriteProviderIds"] = []
            store.create("users", user["id"], user)

        for provider in providers:
            provider["id"] = provider_map[str(provider["id"])]
            if provider.get("ownerId"):
                provider["ownerId"] = account_map.get(str(provider["ownerId"]))
            store.create("providers", provider["id"], provider)

        for migrated in source.export_all("users"):
            user_id = account_map[str(migrated["id"])]
            imported_user = store.get("users", user_id)
            imported_user["favoriteProviderIds"] = [
                provider_map[str(provider_id)]
                for provider_id in migrated.get("favoriteProviderIds", [])
                if str(provider_id) in provider_map
            ]
            store.put("users", user_id, imported_user)

        for queue in queues:
            queue["id"] = _uuid(queue["id"])
            queue["providerId"] = provider_map[str(queue["providerId"])]
            if queue.get("customerId"):
                queue["customerId"] = account_map[str(queue["customerId"])]
            store.create("queues", queue["id"], queue)

        for review in reviews:
            review["id"] = _uuid(review["id"])
            review["providerId"] = provider_map[str(review["providerId"])]
            store.create("provider_reviews", review["id"], review)

    return {
        "accounts": len(users),
        "providers": len(providers),
        "queue_visits": len(queues),
        "provider_reviews": len(reviews),
    }


def main():
    parser = argparse.ArgumentParser(
        description="Apply SmartQueue's Postgres compatibility migration and optionally import JSON data."
    )
    parser.add_argument(
        "--import-json",
        action="store_true",
        help="Import existing data/store records after applying the additive schema migration.",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT / "data" / "store",
        help="Local JSON store path (default: data/store).",
    )
    args = parser.parse_args()
    dsn = os.environ.get("SMARTQUEUE_DATABASE_URL")
    if not dsn:
        parser.error("Set SMARTQUEUE_DATABASE_URL in this shell before running.")

    store = PostgresStore(dsn, os.environ.get("SMARTQUEUE_DB_SSL_ROOT_CERT"))
    with store.transaction():
        migration_sql = MIGRATION.read_text(encoding="utf-8")
        for statement in migration_sql.split(";"):
            if statement.strip():
                store._local.connection.execute(statement)
    print("Postgres compatibility migration applied.")

    if args.import_json:
        ensure_empty_target(store)
        counts = import_json(store, args.data_dir)
        print(
            "Imported JSON records: "
            + ", ".join(f"{key}={value}" for key, value in counts.items())
        )
        print(f"Source records remain unchanged at {args.data_dir}.")


if __name__ == "__main__":
    main()
