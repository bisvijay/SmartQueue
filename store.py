import json
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path


_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class FileStore:
    """Local JSON file store using one file per record and an index per collection."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self._lock = threading.RLock()

    @contextmanager
    def transaction(self):
        with self._lock:
            yield

    def _collection_dir(self, collection):
        if not isinstance(collection, str) or not _SEGMENT.fullmatch(collection):
            raise ValueError("Invalid collection.")
        return self.data_dir / collection

    def _record_path(self, collection, record_id):
        if not isinstance(record_id, str) or not _SEGMENT.fullmatch(record_id):
            raise ValueError("Invalid record ID.")
        return self._collection_dir(collection) / f"{record_id}.json"

    @staticmethod
    def _read_json(file_path):
        try:
            with file_path.open(encoding="utf-8") as file:
                return json.load(file)
        except FileNotFoundError:
            return None

    @staticmethod
    def _write_json(file_path, data):
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=file_path.parent,
                prefix=f".{file_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as file:
                temp_path = Path(file.name)
                json.dump(data, file, ensure_ascii=False, indent=2)
                file.write("\n")
            os.replace(temp_path, file_path)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _read_index(self, collection):
        index = self._read_json(self._collection_dir(collection) / "_index.json")
        if index is None:
            return []
        if not isinstance(index, list):
            raise ValueError(f"Invalid index for {collection}.")
        return index

    def get(self, collection, record_id):
        with self._lock:
            return self._read_json(self._record_path(collection, record_id))

    def list(self, collection):
        with self._lock:
            entries = self._read_index(collection)
            if entries and all(str(entry.get("id", "")).isdigit() for entry in entries):
                return sorted(entries, key=lambda entry: int(entry["id"]))
            return entries

    def put(self, collection, record_id, data, index_entry=None):
        if index_entry is None:
            index_entry = data
        with self._lock:
            self._write_json(self._record_path(collection, record_id), data)
            index = self._read_index(collection)
            for position, item in enumerate(index):
                if str(item.get("id")) == record_id:
                    index[position] = index_entry
                    break
            else:
                index.append(index_entry)
            self._write_json(self._collection_dir(collection) / "_index.json", index)

    def create(self, collection, record_id, data, index_entry=None):
        if index_entry is None:
            index_entry = data
        with self._lock:
            record_path = self._record_path(collection, record_id)
            if record_path.exists():
                return False
            index = self._read_index(collection)
            self._write_json(record_path, data)
            index.append(index_entry)
            self._write_json(self._collection_dir(collection) / "_index.json", index)
            return True

    def delete(self, collection, record_id):
        with self._lock:
            record_path = self._record_path(collection, record_id)
            record_path.unlink(missing_ok=True)
            index = self._read_index(collection)
            index = [item for item in index if str(item.get("id")) != record_id]
            self._write_json(self._collection_dir(collection) / "_index.json", index)

    def get_config(self, relative_path):
        with self._lock:
            return self._read_json(self._config_path(relative_path))

    def put_config(self, relative_path, data):
        with self._lock:
            self._write_json(self._config_path(relative_path), data)

    def export_all(self, collection):
        with self._lock:
            collection_dir = self._collection_dir(collection)
            if not collection_dir.exists():
                return []
            return [
                self._read_json(file_path)
                for file_path in sorted(collection_dir.glob("*.json"))
                if file_path.name != "_index.json"
            ]

    def _config_path(self, relative_path):
        if not isinstance(relative_path, str) or not relative_path.endswith(".json"):
            raise ValueError("Invalid config path.")
        parts = relative_path.replace("\\", "/").split("/")
        if any(part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.json)?", part) for part in parts):
            raise ValueError("Invalid config path.")
        return self.data_dir.joinpath(*parts)
