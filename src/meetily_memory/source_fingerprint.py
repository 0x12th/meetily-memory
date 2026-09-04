from __future__ import annotations

from pathlib import Path
from typing import Any

from meetily_memory.json_codec import dumps_json, loads_json

SOURCE_FINGERPRINT_VERSION = 1
_SOURCE_FILE_SUFFIXES = {"main": "", "wal": "-wal", "journal": "-journal"}


def capture_source_fingerprint(source_path: Path) -> str:
    canonical = Path(source_path).resolve(strict=True)
    files = {
        name: _file_fingerprint(canonical.with_name(canonical.name + suffix))
        for name, suffix in _SOURCE_FILE_SUFFIXES.items()
    }
    return dumps_json({"files": files, "version": SOURCE_FINGERPRINT_VERSION})


def validate_source_fingerprint(value: str) -> str:
    try:
        payload = loads_json(value)
    except ValueError as exc:
        message = "Source fingerprint is not valid JSON."
        raise ValueError(message) from exc
    if not isinstance(payload, dict) or set(payload) != {"files", "version"}:
        message = "Source fingerprint must contain exactly files and version."
        raise ValueError(message)
    if payload["version"] != SOURCE_FINGERPRINT_VERSION:
        message = f"Unsupported source fingerprint version: {payload['version']!r}."
        raise ValueError(message)
    files = payload["files"]
    if not isinstance(files, dict) or set(files) != set(_SOURCE_FILE_SUFFIXES):
        message = "Source fingerprint files are incomplete."
        raise ValueError(message)
    for name, fingerprint in files.items():
        _validate_file_fingerprint(name, fingerprint)
    canonical = dumps_json(payload)
    if canonical != value:
        message = "Source fingerprint is not canonically encoded."
        raise ValueError(message)
    return canonical


def _file_fingerprint(path: Path) -> dict[str, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return {
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def _validate_file_fingerprint(name: object, value: Any) -> None:  # noqa: ANN401
    if value is None:
        if name == "main":
            message = "Source fingerprint main file must exist."
            raise ValueError(message)
        return
    expected = {"device", "inode", "mtime_ns", "size"}
    if not isinstance(value, dict) or set(value) != expected:
        message = f"Source fingerprint {name!r} file metadata is invalid."
        raise ValueError(message)
    invalid_number = any(
        not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in value.values()
    )
    if invalid_number:
        message = f"Source fingerprint {name!r} file metadata must use non-negative integers."
        raise ValueError(message)
