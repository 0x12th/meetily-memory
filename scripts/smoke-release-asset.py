#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

SMOKE_SCHEMA_VERSION = 1
QUERY = "packagedsmokemarker"
MEETING_ID = "synthetic-release-smoke-meeting"
TRANSCRIPT_ID = "synthetic-release-smoke-transcript"
EXPECTED_LAYOUT = {"CHANGELOG.md", "LICENSE", "README.md", "_internal", "mm"}
COMMAND_TIMEOUT_SECONDS = 60


class SmokeFailure(RuntimeError):
    pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Smoke-test an exact packaged Meetily Memory release archive."
    )
    parser.add_argument("archive", type=Path, help="Exact packaged .tar.gz archive")
    parser.add_argument("expected_tag", help="Expected release version or v-prefixed tag")
    parser.add_argument(
        "--result",
        type=Path,
        help="Machine-readable result path (default: <archive>.smoke.json)",
    )
    return parser.parse_args()


def normalize_release_version(tag: str) -> str:
    version = tag.removeprefix("v")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[A-Za-z0-9.-]*)", version):
        message = f"Invalid release version/tag: {tag!r}"
        raise SmokeFailure(message)
    return version


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_member(member: tarfile.TarInfo, expected_root: str) -> None:
    member_path = PurePosixPath(member.name)
    if member_path.is_absolute() or ".." in member_path.parts:
        message = f"Archive contains an unsafe path: {member.name}"
        raise SmokeFailure(message)
    if not member_path.parts or member_path.parts[0] != expected_root:
        message = f"Archive member is outside expected root {expected_root}: {member.name}"
        raise SmokeFailure(message)
    if member.isdev() or member.isfifo():
        message = f"Archive contains an unsupported special file: {member.name}"
        raise SmokeFailure(message)


def extract_archive(archive: Path, extraction_root: Path) -> tuple[Path, Path]:
    if not archive.is_file() or not archive.name.endswith(".tar.gz"):
        message = f"Release archive must be an existing .tar.gz file: {archive}"
        raise SmokeFailure(message)
    if any(extraction_root.iterdir()):
        message = f"Extraction root is not empty: {extraction_root}"
        raise SmokeFailure(message)

    expected_root = archive.name.removesuffix(".tar.gz")
    try:
        with tarfile.open(archive, mode="r:gz") as bundle:
            members = bundle.getmembers()
            if not members:
                message = "Release archive is empty."
                raise SmokeFailure(message)
            for member in members:
                validate_member(member, expected_root)
            bundle.extractall(extraction_root, members=members, filter="data")
    except (OSError, tarfile.TarError) as exc:
        message = f"Could not extract release archive: {exc}"
        raise SmokeFailure(message) from exc

    roots = list(extraction_root.iterdir())
    if roots != [extraction_root / expected_root] or not roots[0].is_dir():
        message = f"Archive must contain exactly one top-level directory named {expected_root}."
        raise SmokeFailure(message)
    bundle_root = roots[0]
    return bundle_root, validate_bundle_layout(bundle_root)


def validate_bundle_layout(bundle_root: Path) -> Path:
    actual_layout = {path.name for path in bundle_root.iterdir()}
    if actual_layout != EXPECTED_LAYOUT:
        missing = sorted(EXPECTED_LAYOUT - actual_layout)
        unexpected = sorted(actual_layout - EXPECTED_LAYOUT)
        message = f"Unexpected bundle layout; missing={missing}, unexpected={unexpected}"
        raise SmokeFailure(message)
    internal_dir = bundle_root / "_internal"
    if not internal_dir.is_dir() or internal_dir.is_symlink():
        message = "Bundle _internal entry is not a directory."
        raise SmokeFailure(message)
    for document in ("README.md", "CHANGELOG.md", "LICENSE"):
        if not (bundle_root / document).is_file():
            message = f"Bundle {document} entry is not a regular file."
            raise SmokeFailure(message)

    binary = bundle_root / "mm"
    if not binary.is_file() or binary.is_symlink():
        message = "Bundle mm entry is not a regular file."
        raise SmokeFailure(message)
    if not binary.stat().st_mode & stat.S_IXUSR or not os.access(binary, os.X_OK):
        message = "Packaged mm binary does not have its executable bit set."
        raise SmokeFailure(message)
    return binary


def create_fixture(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                folder_path TEXT
            );
            CREATE TABLE transcripts (
                id TEXT PRIMARY KEY,
                meeting_id TEXT NOT NULL,
                transcript TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                audio_start_time REAL,
                audio_end_time REAL,
                speaker TEXT
            );
            CREATE TABLE summary_processes (
                meeting_id TEXT PRIMARY KEY,
                result TEXT
            );
            CREATE TABLE meeting_notes (
                meeting_id TEXT PRIMARY KEY,
                notes_markdown TEXT
            );
            """
        )
        connection.execute(
            """
            INSERT INTO meetings (id, title, created_at, updated_at, folder_path)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                MEETING_ID,
                "Synthetic packaged binary smoke",
                "2026-01-01T10:00:00Z",
                "2026-01-01T10:15:00Z",
                str(path.parent / "synthetic-meeting"),
            ),
        )
        connection.execute(
            """
            INSERT INTO transcripts (
                id, meeting_id, transcript, timestamp, audio_start_time, audio_end_time, speaker
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                TRANSCRIPT_ID,
                MEETING_ID,
                f"Synthetic release validation contains {QUERY} evidence.",
                "10:05:00",
                300.0,
                310.0,
                "Synthetic Speaker",
            ),
        )
        connection.commit()


def clean_environment(root: Path) -> dict[str, str]:
    data_dir = root / "data"
    home_dir = root / "home"
    temp_dir = root / "tmp"
    for directory in (data_dir, home_dir, temp_dir):
        directory.mkdir()
    return {
        "HOME": str(home_dir),
        "LANG": "C",
        "LC_ALL": "C",
        "MEETILY_MEMORY_DATA_DIR": str(data_dir),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONNOUSERSITE": "1",
        "TMPDIR": str(temp_dir),
    }


def run_command(
    binary: Path,
    arguments: list[str],
    *,
    cwd: Path,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            [str(binary), *arguments],
            cwd=cwd,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        message = f"Could not run mm {' '.join(arguments)}: {exc}"
        raise SmokeFailure(message) from exc
    if completed.returncode != 0:
        stderr = completed.stderr.strip()
        message = f"mm {' '.join(arguments)} exited with {completed.returncode}" + (
            f": {stderr}" if stderr else ""
        )
        raise SmokeFailure(message)
    return completed


def parse_json_output(completed: subprocess.CompletedProcess[str], command: str) -> object:
    try:
        return cast("object", json.loads(completed.stdout))
    except json.JSONDecodeError as exc:
        message = f"mm {command} did not return valid JSON: {exc}"
        raise SmokeFailure(message) from exc


def require(condition: object, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def json_object(value: object, message: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SmokeFailure(message)
    return cast("dict[str, object]", value)


def json_array(value: object, message: str) -> list[object]:
    if not isinstance(value, list):
        raise SmokeFailure(message)
    return cast("list[object]", value)


def validate_init(payload: object, fixture: Path) -> str:
    payload = json_object(payload, "mm init JSON must be an object.")
    require(payload.get("initialized") is True, "mm init did not report initialized=true.")
    require(payload.get("source_path") == str(fixture), "mm init reported the wrong source path.")
    require(payload.get("meetings_seen") == 1, "mm init did not scan exactly one meeting.")
    require(payload.get("meetings_inserted") == 1, "mm init did not insert exactly one meeting.")
    require(payload.get("chunks_seen") == 1, "mm init did not scan exactly one source chunk.")
    source_uuid = payload.get("source_uuid")
    if not isinstance(source_uuid, str) or not source_uuid:
        message = "mm init omitted source_uuid."
        raise SmokeFailure(message)
    return source_uuid


def validate_search(payload: object, source_uuid: str) -> None:
    results = json_array(payload, "mm s JSON must be an array.")
    require(bool(results), "mm s returned no FTS results.")
    result = json_object(results[0], "mm s result must be an object.")
    meeting = json_object(result.get("meeting"), "mm s result omitted meeting evidence.")
    meeting_ref = json_object(
        meeting.get("ref"),
        "mm s result omitted meeting source identity.",
    )
    require(meeting_ref.get("source_uuid") == source_uuid, "mm s source UUID differs from init.")
    require(
        meeting_ref.get("external_id") == MEETING_ID,
        "mm s result does not reference the synthetic source meeting.",
    )
    evidence = json_array(result.get("evidence"), "mm s evidence must be an array.")
    require(bool(evidence), "mm s result omitted source evidence.")
    evidence_item = json_object(evidence[0], "mm s evidence must be an object.")
    excerpt = json_object(
        evidence_item.get("excerpt"),
        "mm s evidence omitted its source excerpt.",
    )
    require(
        excerpt.get("chunk_external_id") == TRANSCRIPT_ID,
        "mm s returned the wrong source chunk.",
    )
    require(
        QUERY in str(excerpt.get("text", "")).casefold(),
        "mm s evidence omitted the FTS marker.",
    )


def validate_doctor(payload: object, fixture: Path) -> None:
    payload = json_object(payload, "mm doctor JSON must be an object.")
    require(payload.get("source_path") == str(fixture), "mm doctor reported the wrong source path.")
    require(payload.get("source_readable") is True, "mm doctor could not read the source fixture.")
    require(payload.get("source_schema_valid") is True, "mm doctor rejected the source schema.")
    require(payload.get("fts5") is True, "Packaged SQLite runtime does not provide FTS5.")
    require(payload.get("meetings") == 1, "mm doctor did not observe one indexed meeting.")
    require(payload.get("chunks") == 1, "mm doctor did not observe one indexed chunk.")
    index_database = json_object(
        payload.get("index_database"),
        "mm doctor omitted the index database diagnostic.",
    )
    state_database = json_object(
        payload.get("state_database"),
        "mm doctor omitted the state database diagnostic.",
    )
    require(
        index_database.get("status") == "current",
        "mm doctor did not observe a current index database.",
    )
    require(
        state_database.get("status") == "current",
        "mm doctor did not observe a current state database.",
    )
    completed_run = json_object(
        payload.get("last_completed_run"),
        "mm doctor omitted the completed source scan.",
    )
    require(
        completed_run.get("status") == "completed",
        "mm doctor omitted the completed source scan.",
    )


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def sqlite_row_counts(path: Path) -> dict[str, int]:
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            tables = [
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
                )
            ]
            return {
                table: int(
                    connection.execute(
                        f"SELECT COUNT(*) FROM {quote_identifier(table)}"
                    ).fetchone()[0]
                )
                for table in tables
            }
    except sqlite3.Error as exc:
        message = f"Could not snapshot SQLite rows for {path}: {exc}"
        raise SmokeFailure(message) from exc


def workspace_snapshot(root: Path) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    rows: dict[str, dict[str, int]] = {}
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        relative_path = str(path.relative_to(root))
        details: dict[str, Any] = {
            "sha256": sha256_file(path),
            "mtime_ns": path.stat().st_mtime_ns,
            "size": path.stat().st_size,
        }
        files[relative_path] = details
        if path.suffix == ".sqlite":
            rows[relative_path] = sqlite_row_counts(path)
    return {"files": files, "rows": rows}


def snapshot_digest(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def run_smoke(archive: Path, expected_tag: str, result: dict[str, Any]) -> None:
    archive = archive.resolve(strict=False)
    expected_version = normalize_release_version(expected_tag)
    result["archive"] = archive.name
    result["archive_sha256"] = sha256_file(archive)
    result["expected_tag"] = expected_tag
    result["expected_version"] = expected_version

    with tempfile.TemporaryDirectory(prefix="meetily-memory-release-smoke-") as raw_root:
        smoke_root = Path(raw_root).resolve()
        invocation_cwd = Path.cwd().resolve()
        require(
            smoke_root != invocation_cwd
            and invocation_cwd not in smoke_root.parents
            and smoke_root not in invocation_cwd.parents,
            "Smoke temporary root must be outside the invocation working directory.",
        )
        extraction_root = smoke_root / "extract"
        extraction_root.mkdir()
        bundle_root, binary = extract_archive(archive, extraction_root)
        result["bundle_root"] = bundle_root.name
        result["checks"]["layout"] = True
        result["checks"]["executable"] = True

        work_dir = smoke_root / "work"
        work_dir.mkdir()
        fixture = smoke_root / "fixture" / "meeting_minutes.sqlite"
        fixture.parent.mkdir()
        create_fixture(fixture)
        result["checks"]["fixture"] = True

        environment = clean_environment(smoke_root)
        version = run_command(binary, ["--version"], cwd=work_dir, environment=environment)
        expected_output = f"meetily-memory {expected_version}"
        require(
            version.stdout.splitlines() == [expected_output],
            "Packaged binary version does not match tag.",
        )
        result["checks"]["version"] = True

        help_result = run_command(binary, ["--help"], cwd=work_dir, environment=environment)
        require(bool(help_result.stdout.strip()), "Packaged binary --help returned empty output.")
        require(
            "init" in help_result.stdout and "doctor" in help_result.stdout,
            "Incomplete --help output.",
        )
        result["checks"]["help"] = True

        init_result = run_command(
            binary,
            ["init", "--source", str(fixture), "--no-autosync", "--json"],
            cwd=work_dir,
            environment=environment,
        )
        init_payload = parse_json_output(init_result, "init")
        source_uuid = validate_init(init_payload, fixture)
        result["checks"]["init"] = True

        search_result = run_command(
            binary,
            ["s", QUERY, "--json"],
            cwd=work_dir,
            environment=environment,
        )
        validate_search(parse_json_output(search_result, "s"), source_uuid)
        result["checks"]["fts_search"] = True

        before_doctor = workspace_snapshot(smoke_root)
        doctor_result = run_command(
            binary,
            ["doctor", "--source", str(fixture), "--json"],
            cwd=work_dir,
            environment=environment,
        )
        validate_doctor(parse_json_output(doctor_result, "doctor"), fixture)
        after_doctor = workspace_snapshot(smoke_root)
        require(after_doctor == before_doctor, "mm doctor modified files or SQLite rows.")
        result["checks"]["doctor"] = True
        result["checks"]["doctor_read_only"] = True
        result["doctor_snapshot_sha256"] = snapshot_digest(before_doctor)
        result["evidence"] = {
            "chunks": 1,
            "meeting_external_id": MEETING_ID,
            "query": QUERY,
            "source_uuid": source_uuid,
        }
        result["isolation"] = {
            "cwd_outside_checkout": True,
            "data_dir_isolated": True,
            "project_python_environment_removed": True,
        }


def write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary_path.replace(path)


def main() -> int:
    arguments = parse_args()
    archive = arguments.archive.resolve(strict=False)
    result_path = (
        arguments.result.resolve(strict=False)
        if arguments.result is not None
        else archive.with_name(f"{archive.name}.smoke.json")
    )
    result: dict[str, Any] = {
        "checks": {},
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "schema_version": SMOKE_SCHEMA_VERSION,
        "status": "failed",
    }
    try:
        run_smoke(archive, arguments.expected_tag, result)
    except (OSError, SmokeFailure, sqlite3.Error) as exc:
        result["error"] = str(exc)
        write_result(result_path, result)
        sys.stderr.write(f"release smoke failed: {exc}\n")
        sys.stdout.write(f"{result_path}\n")
        return 1

    result["status"] = "passed"
    write_result(result_path, result)
    sys.stdout.write(f"{result_path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
