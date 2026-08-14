import json
import os
import time
from contextlib import contextmanager
from typing import Any, Callable, TypeVar

T = TypeVar("T")
LOCK_STALE_SEC = 30.0


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
