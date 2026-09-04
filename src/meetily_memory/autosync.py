from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from meetily_memory.durable_files import durable_replace

AUTOSYNC_LABEL = "com.meetily-memory.autosync"
AUTOSYNC_MINUTES = (0, 15, 30, 45)
PROGRAM_ARGUMENT_COUNT = 5
AutosyncState = Literal["enabled", "disabled", "other-workspace", "broken"]


class AutosyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class AutosyncStatus:
    state: AutosyncState
    current_index: Path
    configured_index: Path | None
    plist_path: Path
    loaded: bool
    last_exit_code: int | None
    stderr_log: Path | None

    def as_payload(self) -> dict[str, object]:
        return {
            "state": self.state,
            "scheduler": "launchd",
            "schedule": [f":{minute:02d}" for minute in AUTOSYNC_MINUTES],
            "current_index": str(self.current_index),
            "configured_index": (
                str(self.configured_index) if self.configured_index is not None else None
            ),
            "plist_path": str(self.plist_path),
            "loaded": self.loaded,
            "last_exit_code": self.last_exit_code,
            "stderr_log": str(self.stderr_log) if self.stderr_log is not None else None,
        }


def launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def autosync_plist_path() -> Path:
    return launch_agents_dir() / f"{AUTOSYNC_LABEL}.plist"


def launchd_target() -> str:
    return f"gui/{os.getuid()}"


def launchd_service() -> str:
    return f"{launchd_target()}/{AUTOSYNC_LABEL}"


def status(index_path: Path) -> AutosyncStatus:
    _require_macos()
    current = Path(index_path).expanduser().absolute()
    plist_path = autosync_plist_path()
    parsed = _read_plist(plist_path) if plist_path.is_file() else None
    configured = _configured_index(parsed)
    stderr_log = _stderr_log(parsed)
    printed = _launchctl("print", launchd_service())
    loaded = printed.returncode == 0
    last_exit_code = _last_exit_code(printed.stdout) if loaded else None
    valid = parsed is not None and _valid_plist(parsed, configured)
    if parsed is None and not loaded:
        state: AutosyncState = "disabled"
    elif not valid or not loaded:
        state = "broken"
    elif configured != current:
        state = "other-workspace"
    else:
        state = "enabled"
    return AutosyncStatus(
        state=state,
        current_index=current,
        configured_index=configured,
        plist_path=plist_path,
        loaded=loaded,
        last_exit_code=last_exit_code,
        stderr_log=stderr_log,
    )


def enable(index_path: Path, *, replace: bool = False) -> AutosyncStatus:
    _require_macos()
    current = status(index_path)
    if current.state == "enabled":
        return current
    owns_another_workspace = (
        current.configured_index is not None and current.configured_index != current.current_index
    )
    if owns_another_workspace and not replace:
        message = (
            f"Autosync already serves {current.configured_index}. "
            "Rerun with --replace to switch workspaces."
        )
        raise AutosyncError(message)
    executable = _stable_mm_executable()
    index = Path(index_path).expanduser().absolute()
    plist_path = autosync_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    payload = build_launch_agent_plist(executable, index)
    _launchctl("bootout", launchd_service())
    _write_plist(plist_path, payload)
    bootstrapped = _launchctl("bootstrap", launchd_target(), str(plist_path))
    if bootstrapped.returncode != 0:
        detail = bootstrapped.stderr.strip() or bootstrapped.stdout.strip() or "unknown error"
        message = f"launchctl bootstrap failed: {detail}"
        raise AutosyncError(message)
    result = status(index)
    if result.state != "enabled":
        message = f"LaunchAgent was installed but its state is {result.state}."
        raise AutosyncError(message)
    return result


def disable(index_path: Path) -> AutosyncStatus:
    _require_macos()
    current = status(index_path)
    owns_another_workspace = (
        current.configured_index is not None and current.configured_index != current.current_index
    )
    if owns_another_workspace:
        message = (
            f"Autosync serves another workspace: {current.configured_index}. "
            "Run disable with that index path."
        )
        raise AutosyncError(message)
    if current.state == "disabled":
        return current
    bootout = _launchctl("bootout", launchd_service())
    if bootout.returncode != 0 and current.loaded:
        detail = bootout.stderr.strip() or bootout.stdout.strip() or "unknown error"
        message = f"launchctl bootout failed: {detail}"
        raise AutosyncError(message)
    autosync_plist_path().unlink(missing_ok=True)
    result = status(index_path)
    if result.state != "disabled":
        message = f"LaunchAgent removal left autosync in state {result.state}."
        raise AutosyncError(message)
    return result


def build_launch_agent_plist(executable: Path, index_path: Path) -> dict[str, object]:
    return {
        "Label": AUTOSYNC_LABEL,
        "ProgramArguments": [
            str(executable),
            "--index",
            str(index_path),
            "refresh",
            "--sync-obsidian",
        ],
        "StartCalendarInterval": [{"Minute": minute} for minute in AUTOSYNC_MINUTES],
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": str(index_path.parent / "autosync.stderr.log"),
        "ProcessType": "Background",
    }


def _write_plist(path: Path, payload: dict[str, object]) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            plistlib.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        durable_replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_plist(path: Path) -> dict[str, object] | None:
    try:
        with path.open("rb") as stream:
            value = plistlib.load(stream)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _configured_index(payload: dict[str, object] | None) -> Path | None:
    if payload is None:
        return None
    raw_arguments = payload.get("ProgramArguments")
    if not isinstance(raw_arguments, list) or not all(
        isinstance(item, str) for item in raw_arguments
    ):
        return None
    arguments = cast("list[str]", raw_arguments)
    if "--index" not in arguments:
        return None
    position = arguments.index("--index") + 1
    if position >= len(arguments) or not isinstance(arguments[position], str):
        return None
    return Path(arguments[position]).expanduser().absolute()


def _stderr_log(payload: dict[str, object] | None) -> Path | None:
    if payload is None or not isinstance(payload.get("StandardErrorPath"), str):
        return None
    return Path(str(payload["StandardErrorPath"]))


def _valid_plist(payload: dict[str, object], configured: Path | None) -> bool:
    arguments = payload.get("ProgramArguments")
    expected_schedule = [{"Minute": minute} for minute in AUTOSYNC_MINUTES]
    return bool(
        payload.get("Label") == AUTOSYNC_LABEL
        and configured is not None
        and isinstance(arguments, list)
        and len(arguments) == PROGRAM_ARGUMENT_COUNT
        and isinstance(arguments[0], str)
        and Path(arguments[0]).is_absolute()
        and arguments[1:] == ["--index", str(configured), "refresh", "--sync-obsidian"]
        and payload.get("StartCalendarInterval") == expected_schedule
        and "RunAtLoad" not in payload
        and payload.get("StandardOutPath") == "/dev/null"
        and payload.get("StandardErrorPath") == str(configured.parent / "autosync.stderr.log")
    )


def _stable_mm_executable() -> Path:
    value = shutil.which("mm")
    if value is None:
        message = "A stable installed `mm` executable was not found on PATH."
        raise AutosyncError(message)
    executable = Path(value).expanduser()
    if not executable.is_absolute():
        message = "The installed `mm` executable path must be absolute."
        raise AutosyncError(message)
    lowered = executable.as_posix().casefold()
    virtual_env = os.environ.get("VIRTUAL_ENV")
    if "/cellar/" in lowered or "/.venv/" in lowered or "/venv/" in lowered:
        message = "Autosync requires a stable installed `mm` executable, not a venv path."
        raise AutosyncError(message)
    if virtual_env and executable.is_relative_to(Path(virtual_env).expanduser().absolute()):
        message = "Autosync requires a stable installed `mm` executable, not VIRTUAL_ENV."
        raise AutosyncError(message)
    return executable


def _launchctl(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        ["/bin/launchctl", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def _last_exit_code(output: str) -> int | None:
    match = re.search(r"last exit code\s*=\s*(-?\d+)", output, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _require_macos() -> None:
    if sys.platform != "darwin":
        message = "Autosync is supported only on macOS."
        raise AutosyncError(message)
