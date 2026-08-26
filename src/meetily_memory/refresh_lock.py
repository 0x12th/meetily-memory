from __future__ import annotations

import fcntl
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Self, TextIO

from meetily_memory.json_codec import dumps_json, loads_json


class RefreshLockBusyError(RuntimeError):
    pass


class RefreshLock:
    def __init__(self, index_path: Path) -> None:
        self.path = Path(index_path).parent / "refresh.lock"
        self._file: TextIO | None = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = read_lock_owner(lock_file)
            lock_file.close()
            raise RefreshLockBusyError(format_busy_message(owner)) from exc

        acquired_at = utc_now()
        lock_file.seek(0)
        lock_file.truncate()
        lock_file.write(dumps_json({"pid": os.getpid(), "acquired_at": acquired_at}) + "\n")
        lock_file.flush()
        os.fsync(lock_file.fileno())
        self._file = lock_file
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        if self._file is None:
            return
        fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        self._file.close()
        self._file = None


def read_lock_owner(lock_file: TextIO) -> dict[str, object]:
    try:
        lock_file.seek(0)
        payload = loads_json(lock_file.read())
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def format_busy_message(owner: dict[str, object]) -> str:
    pid = owner.get("pid", "unknown")
    acquired_at = owner.get("acquired_at", "unknown")
    return f"Refresh is already running (PID {pid}, acquired at {acquired_at})."


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
