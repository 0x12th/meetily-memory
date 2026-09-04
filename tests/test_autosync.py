from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from meetily_memory import autosync


def completed(
    returncode: int,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["launchctl"], returncode, stdout, stderr)


def install_plist(path: Path, index_path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        plistlib.dump(
            autosync.build_launch_agent_plist(Path("/opt/homebrew/bin/mm"), index_path),
            stream,
        )


def test_plist_uses_one_quarter_hour_job_without_run_at_load(tmp_path: Path) -> None:
    index_path = (tmp_path / "index.sqlite").absolute()

    payload = autosync.build_launch_agent_plist(Path("/opt/homebrew/bin/mm"), index_path)

    assert payload["ProgramArguments"] == [
        "/opt/homebrew/bin/mm",
        "--index",
        str(index_path),
        "refresh",
        "--sync-obsidian",
    ]
    assert payload["StartCalendarInterval"] == [
        {"Minute": 0},
        {"Minute": 15},
        {"Minute": 30},
        {"Minute": 45},
    ]
    assert "RunAtLoad" not in payload
    assert payload["StandardOutPath"] == "/dev/null"
    assert payload["StandardErrorPath"] == str(tmp_path / "autosync.stderr.log")


def test_status_distinguishes_enabled_other_workspace_disabled_and_broken(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plist_path = tmp_path / "agent.plist"
    current = (tmp_path / "current" / "index.sqlite").absolute()
    other = (tmp_path / "other" / "index.sqlite").absolute()
    monkeypatch.setattr(autosync.sys, "platform", "darwin")
    monkeypatch.setattr(autosync, "autosync_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        autosync,
        "_launchctl",
        lambda *_args: completed(0, "last exit code = 0"),
    )

    install_plist(plist_path, current)
    assert autosync.status(current).state == "enabled"
    assert autosync.status(other).state == "other-workspace"

    plist_path.unlink()
    monkeypatch.setattr(autosync, "_launchctl", lambda *_args: completed(1))
    assert autosync.status(current).state == "disabled"

    install_plist(plist_path, current)
    assert autosync.status(current).state == "broken"


def test_enable_requires_replace_and_disable_refuses_other_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plist_path = tmp_path / "agent.plist"
    current = (tmp_path / "current" / "index.sqlite").absolute()
    other = (tmp_path / "other" / "index.sqlite").absolute()
    install_plist(plist_path, other)
    monkeypatch.setattr(autosync.sys, "platform", "darwin")
    monkeypatch.setattr(autosync, "autosync_plist_path", lambda: plist_path)
    monkeypatch.setattr(
        autosync,
        "_launchctl",
        lambda *_args: completed(0, "last exit code = 0"),
    )

    with pytest.raises(autosync.AutosyncError, match="--replace"):
        autosync.enable(current)
    with pytest.raises(autosync.AutosyncError, match="another workspace"):
        autosync.disable(current)

    assert autosync.status(other).state == "enabled"


def test_enable_and_disable_manage_one_launch_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plist_path = tmp_path / "agent.plist"
    index_path = (tmp_path / "index.sqlite").absolute()
    loaded = False

    def fake_launchctl(action: str, *_args: str) -> subprocess.CompletedProcess[str]:
        nonlocal loaded
        if action == "bootstrap":
            loaded = True
            return completed(0)
        if action == "bootout":
            loaded = False
            return completed(0)
        if action == "print":
            return completed(0, "last exit code = 0") if loaded else completed(1)
        raise AssertionError(action)

    monkeypatch.setattr(autosync.sys, "platform", "darwin")
    monkeypatch.setattr(autosync, "autosync_plist_path", lambda: plist_path)
    monkeypatch.setattr(autosync, "_stable_mm_executable", lambda: Path("/opt/homebrew/bin/mm"))
    monkeypatch.setattr(autosync, "_launchctl", fake_launchctl)

    assert autosync.enable(index_path).state == "enabled"
    assert plist_path.is_file()
    assert autosync.disable(index_path).state == "disabled"
    assert not plist_path.exists()
