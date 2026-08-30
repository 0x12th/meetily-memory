import errno
import os
import sqlite3
import stat
from pathlib import Path

import pytest

import meetily_memory.durable_files as durable_files_module
import meetily_memory.scanner.meetily_sqlite as scanner_module
from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION
from meetily_memory.durable_files import durable_replace
from meetily_memory.scanner.meetily_sqlite import (
    MeetilySQLiteScanner,
    previous_index_backup_path,
)


def test_durable_replace_orders_file_fsync_replace_and_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_path = tmp_path / "index.rebuild"
    destination_path = tmp_path / "index.sqlite"
    _ = temporary_path.write_bytes(b"rebuilt")
    _ = destination_path.write_bytes(b"original")
    events: list[str] = []
    real_fsync = os.fsync
    real_replace = os.replace

    def recording_fsync(file_descriptor: int) -> None:
        mode = os.fstat(file_descriptor).st_mode
        if stat.S_ISREG(mode):
            events.append("file_fsync")
        elif stat.S_ISDIR(mode):
            events.append("directory_fsync")
        real_fsync(file_descriptor)

    def recording_replace(source: Path, destination: Path) -> None:
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    monkeypatch.setattr(os, "replace", recording_replace)

    durable_replace(temporary_path, destination_path)

    assert events == ["file_fsync", "replace", "directory_fsync"]
    assert destination_path.read_bytes() == b"rebuilt"


def test_durable_replace_file_fsync_failure_leaves_destination_untouched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_path = tmp_path / "index.rebuild"
    destination_path = tmp_path / "index.sqlite"
    _ = temporary_path.write_bytes(b"rebuilt")
    _ = destination_path.write_bytes(b"original")
    real_fsync = os.fsync

    def fail_regular_file_fsync(file_descriptor: int) -> None:
        if stat.S_ISREG(os.fstat(file_descriptor).st_mode):
            message = "file fsync failed"
            raise OSError(message)
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", fail_regular_file_fsync)

    with pytest.raises(OSError, match="file fsync failed"):
        durable_replace(temporary_path, destination_path)

    assert destination_path.read_bytes() == b"original"
    assert temporary_path.read_bytes() == b"rebuilt"


def test_durable_replace_ignores_unsupported_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_path = tmp_path / "index.rebuild"
    destination_path = tmp_path / "index.sqlite"
    _ = temporary_path.write_bytes(b"rebuilt")
    _ = destination_path.write_bytes(b"original")
    real_fsync = os.fsync

    def reject_directory_fsync(file_descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            raise OSError(errno.EINVAL, "directory fsync unsupported")
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", reject_directory_fsync)

    durable_replace(temporary_path, destination_path)

    assert destination_path.read_bytes() == b"rebuilt"
    assert not temporary_path.exists()


def test_durable_replace_propagates_directory_io_failure_after_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    temporary_path = tmp_path / "index.rebuild"
    destination_path = tmp_path / "index.sqlite"
    _ = temporary_path.write_bytes(b"rebuilt")
    _ = destination_path.write_bytes(b"original")
    real_fsync = os.fsync

    def fail_directory_fsync(file_descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(file_descriptor).st_mode):
            raise OSError(errno.EIO, "directory fsync failed")
        real_fsync(file_descriptor)

    monkeypatch.setattr(os, "fsync", fail_directory_fsync)

    with pytest.raises(OSError, match="directory fsync failed"):
        durable_replace(temporary_path, destination_path)

    assert destination_path.read_bytes() == b"rebuilt"
    assert not temporary_path.exists()


def test_legacy_rebuild_routes_backup_then_primary_through_durable_replace(
    meetily_db: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    index_path = tmp_path / "index.sqlite"
    scanner = MeetilySQLiteScanner(index_path)
    _ = scanner.scan(meetily_db)
    with sqlite3.connect(index_path) as conn:
        _ = conn.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION - 1}")
        conn.commit()
    destinations: list[Path] = []
    real_durable_replace = durable_files_module.durable_replace

    def recording_durable_replace(temporary_path: Path, destination_path: Path) -> None:
        destinations.append(destination_path)
        real_durable_replace(temporary_path, destination_path)

    monkeypatch.setattr(scanner_module, "durable_replace", recording_durable_replace)

    _ = scanner.scan(meetily_db)

    assert destinations == [previous_index_backup_path(index_path), index_path]
