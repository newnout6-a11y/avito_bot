import json
import os
import sqlite3
import time
from contextlib import closing, contextmanager
from typing import Any, Callable, TypeVar

T = TypeVar("T")
LOCK_STALE_SEC = 30.0


def _sqlite_path() -> str:
    path = os.getenv("DATABASE_FILE", os.path.join("data", "avito_monitor.sqlite3"))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    return path


def _use_sqlite(path: str) -> bool:
    # Absolute paths are intentionally kept as JSON for isolated tests and
    # explicit one-off imports. Relative application paths use SQLite.
    return os.getenv("STORAGE_BACKEND", "sqlite").lower() == "sqlite" and not os.path.isabs(path)


def _connect_sqlite() -> sqlite3.Connection:
    connection = sqlite3.connect(_sqlite_path(), timeout=10.0, isolation_level=None)
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS documents (key TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
    )
    return connection


def _sqlite_load(path: str, default: Any) -> Any:
    key = os.path.normpath(path)
    with closing(_connect_sqlite()) as connection:
        row = connection.execute("SELECT payload FROM documents WHERE key = ?", (key,)).fetchone()
        if row is not None:
            return json.loads(row[0])

        # Import an existing JSON document once, without deleting it.
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                value = json.load(handle)
            connection.execute(
                "INSERT OR IGNORE INTO documents(key, payload, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), time.time()),
            )
            return value
    return default


def _sqlite_save(path: str, data: Any) -> None:
    key = os.path.normpath(path)
    payload = json.dumps(data, ensure_ascii=False)
    with closing(_connect_sqlite()) as connection:
        connection.execute(
            "INSERT INTO documents(key, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (key, payload, time.time()),
        )


def _sqlite_update(path: str, default: Any, mutator: Callable[[Any], T]) -> T:
    key = os.path.normpath(path)
    with closing(_connect_sqlite()) as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT payload FROM documents WHERE key = ?", (key,)).fetchone()
        if row is None:
            data = default
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as handle:
                    data = json.load(handle)
        else:
            data = json.loads(row[0])
        result = mutator(data)
        connection.execute(
            "INSERT INTO documents(key, payload, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (key, json.dumps(data, ensure_ascii=False), time.time()),
        )
        connection.commit()
        return result


def _lock_is_stale(lock_path: str) -> bool:
    try:
        return time.time() - os.path.getmtime(lock_path) >= LOCK_STALE_SEC
    except FileNotFoundError:
        return False


@contextmanager
def _file_lock(path: str, timeout: float = 5.0):
    lock_path = path + ".lock"
    deadline = time.monotonic() + timeout
    fd = None
    while fd is None:
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(lock_path):
                try:
                    os.unlink(lock_path)
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for storage lock: {lock_path}")
            time.sleep(0.05)
    try:
        yield
    finally:
        os.close(fd)
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


def load_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    with _file_lock(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)


def load_state(path: str, default: Any) -> Any:
    if _use_sqlite(path):
        return _sqlite_load(path, default)
    return load_json(path, default)


def save_json(path: str, data: Any) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.tmp"
    with _file_lock(path):
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass


def save_state(path: str, data: Any) -> None:
    if _use_sqlite(path):
        _sqlite_save(path, data)
    else:
        save_json(path, data)


def update_json(path: str, default: Any, mutator: Callable[[Any], T]) -> T:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp_path = f"{path}.{os.getpid()}.tmp"
    with _file_lock(path):
        data = default
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        result = mutator(data)
        try:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
        return result


def update_state(path: str, default: Any, mutator: Callable[[Any], T]) -> T:
    if _use_sqlite(path):
        return _sqlite_update(path, default, mutator)
    return update_json(path, default, mutator)
