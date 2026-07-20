import os
import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from meetily_memory.cli.common import make_typer, print_json, print_text_block
from meetily_memory.config.settings import load_app_settings, update_app_settings

autosync_app = make_typer("Manage automatic index refreshes.")
AUTOSYNC_COMMAND = "refresh"
LAUNCHD_LABEL = "com.meetily-memory.autosync"
SYSTEMD_SERVICE = "meetily-memory-autosync.service"
SYSTEMD_TIMER = "meetily-memory-autosync.timer"
LAUNCHCTL = shutil.which("launchctl") or "/bin/launchctl"


@dataclass(frozen=True)
class AutosyncRuntimeStatus:
    configured: bool
    installed: bool
    active: bool
    scheduler: str
    last_update_at: str | None

    @property
    def enabled(self) -> bool:
        return self.configured and self.installed and self.active

    @property
    def label(self) -> str:
        if self.enabled:
            return "enabled"
        if self.configured or self.installed or self.active:
            return "misconfigured"
        return "disabled"

    def as_payload(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "installed": self.installed,
            "active": self.active,
            "enabled": self.enabled,
            "scheduler": self.scheduler,
            "last_update_at": self.last_update_at,
        }


@autosync_app.command("start")
def autosync_start(
    ctx: typer.Context,
    interval_minutes: Annotated[
        int,
        typer.Option("--interval-minutes", help="Background refresh interval."),
    ] = 30,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    try:
        status = enable_autosync(
            ctx.obj["index_path"],
            ctx.obj["settings_path"],
            interval_minutes=interval_minutes,
        )
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {**status.as_payload(), "interval_minutes": interval_minutes}
    if json_output:
        print_json(payload)
        return
    print_text_block(f"autosync: {status.label}")
    print_text_block(f"scheduler: {status.scheduler}")


@autosync_app.command("stop")
def autosync_stop(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    try:
        removed_files = disable_autosync(ctx.obj["settings_path"])
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {
        "configured": False,
        "enabled": False,
        "removed_files": [str(path) for path in removed_files],
    }
    if json_output:
        print_json(payload)
        return
    print_text_block("autosync: disabled")
    for path in removed_files:
        print_text_block(f"removed: {path}")


@autosync_app.command("status")
def autosync_status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    status = autosync_runtime_status(ctx.obj["settings_path"], ctx.obj["index_path"])
    if json_output:
        print_json(status.as_payload())
        return
    print_text_block(f"autosync: {status.label}")
    print_text_block(f"scheduler: {status.scheduler}")
    print_text_block(f"last refresh: {status.last_update_at or 'never'}")


def enable_autosync(
    index_path: Path,
    settings_path: Path,
    *,
    interval_minutes: int,
) -> AutosyncRuntimeStatus:
    if sys.platform == "darwin":
        plist_path = install_launchd_plist(
            interval_minutes,
            index_path=index_path,
            data_dir=settings_path.parent,
        )
        activate_launchd(plist_path)
    elif sys.platform.startswith("linux"):
        install_systemd_user_units(
            interval_minutes,
            index_path=index_path,
            data_dir=settings_path.parent,
        )
        activate_systemd_timer()
    else:
        message = f"Autosync is not supported on platform: {sys.platform}"
        raise RuntimeError(message)
    update_app_settings(settings_path=settings_path, autosync_enabled=True)
    status = autosync_runtime_status(settings_path, index_path)
    if not status.enabled:
        message = f"Autosync scheduler did not become active: {status.scheduler}"
        raise RuntimeError(message)
    return status


def disable_autosync(settings_path: Path) -> list[Path]:
    if sys.platform == "darwin":
        run_command(
            [LAUNCHCTL, "bootout", launchd_domain_target()],
            allowed_returncodes={0, 3, 113},
        )
    elif sys.platform.startswith("linux"):
        systemctl = shutil.which("systemctl")
        if systemctl:
            run_command(
                [systemctl, "--user", "disable", "--now", SYSTEMD_TIMER],
                allowed_returncodes={0, 1, 5},
            )
    removed_files = remove_autosync_files()
    update_app_settings(settings_path=settings_path, autosync_enabled=False)
    return removed_files


def autosync_runtime_status(
    settings_path: Path, index_path: Path | None = None
) -> AutosyncRuntimeStatus:
    settings = load_app_settings(settings_path)
    if sys.platform == "darwin":
        installed = launchd_plist_matches(
            launchd_plist_path(),
            index_path=index_path,
            data_dir=settings_path.parent,
        )
        active = installed and launchd_is_active()
        scheduler = "launchd"
    elif sys.platform.startswith("linux"):
        installed = systemd_timer_path().is_file() and systemd_service_path().is_file()
        active = installed and systemd_timer_is_active()
        scheduler = "systemd"
    else:
        installed = False
        active = False
        scheduler = "unsupported"
    return AutosyncRuntimeStatus(
        configured=settings.autosync_enabled,
        installed=installed,
        active=active,
        scheduler=scheduler,
        last_update_at=settings.last_update_at,
    )


def install_launchd_plist(
    interval_minutes: int,
    *,
    index_path: Path,
    data_dir: Path,
) -> Path:
    plist_path = launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    seconds = max(interval_minutes, 1) * 60
    executable = autosync_executable()
    stdout_path = data_dir / "autosync.stdout.log"
    stderr_path = data_dir / "autosync.stderr.log"
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{executable}</string>
    <string>--index</string>
    <string>{index_path}</string>
    <string>{AUTOSYNC_COMMAND}</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>MEETILY_MEMORY_DATA_DIR</key>
    <string>{data_dir}</string>
  </dict>
  <key>StandardOutPath</key>
  <string>{stdout_path}</string>
  <key>StandardErrorPath</key>
  <string>{stderr_path}</string>
  <key>StartInterval</key>
  <integer>{seconds}</integer>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
"""
    plist_path.write_text(plist, encoding="utf-8")
    return plist_path


def activate_launchd(plist_path: Path) -> None:
    run_command(
        [LAUNCHCTL, "bootout", launchd_domain_target()],
        allowed_returncodes={0, 3, 113},
    )
    run_command([LAUNCHCTL, "bootstrap", launchd_domain(), str(plist_path)])


def launchd_is_active() -> bool:
    result = subprocess.run(  # noqa: S603
        [LAUNCHCTL, "print", launchd_domain_target()],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def launchd_plist_matches(
    plist_path: Path,
    *,
    index_path: Path | None,
    data_dir: Path,
) -> bool:
    if not plist_path.is_file():
        return False
    try:
        with plist_path.open("rb") as plist_file:
            payload = plistlib.load(plist_file)
    except (OSError, plistlib.InvalidFileException):
        return False
    if not isinstance(payload, dict):
        return False
    environment = payload.get("EnvironmentVariables")
    arguments = payload.get("ProgramArguments")
    matches_shape = isinstance(environment, dict) and isinstance(arguments, list)
    if not matches_shape:
        return False
    matches_data_dir = environment.get("MEETILY_MEMORY_DATA_DIR") == str(data_dir)
    matches_index = index_path is None or ["--index", str(index_path)] == arguments[1:3]
    return matches_data_dir and matches_index


def launchd_domain() -> str:
    return f"gui/{os.getuid()}"


def launchd_domain_target() -> str:
    return f"{launchd_domain()}/{LAUNCHD_LABEL}"


def launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def install_systemd_user_units(
    interval_minutes: int,
    *,
    index_path: Path,
    data_dir: Path,
) -> list[Path]:
    service_path = systemd_service_path()
    timer_path = systemd_timer_path()
    service_path.parent.mkdir(parents=True, exist_ok=True)
    data_dir.mkdir(parents=True, exist_ok=True)
    executable = autosync_executable()
    minutes = max(interval_minutes, 1)
    service = f"""[Unit]
Description=Meetily Memory automatic refresh

[Service]
Type=oneshot
Environment=MEETILY_MEMORY_DATA_DIR={systemd_quote(data_dir)}
ExecStart={systemd_quote(executable)} --index {systemd_quote(index_path)} {AUTOSYNC_COMMAND}
"""
    timer = f"""[Unit]
Description=Run Meetily Memory automatic refresh

[Timer]
OnBootSec=1min
OnUnitActiveSec={minutes}min
Unit={SYSTEMD_SERVICE}

[Install]
WantedBy=timers.target
"""
    service_path.write_text(service, encoding="utf-8")
    timer_path.write_text(timer, encoding="utf-8")
    return [service_path, timer_path]


def activate_systemd_timer() -> None:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        message = "systemctl was not found; autosync could not be activated."
        raise RuntimeError(message)
    run_command([systemctl, "--user", "daemon-reload"])
    run_command([systemctl, "--user", "enable", "--now", SYSTEMD_TIMER])


def systemd_timer_is_active() -> bool:
    systemctl = shutil.which("systemctl")
    if systemctl is None:
        return False
    result = subprocess.run(  # noqa: S603
        [systemctl, "--user", "is-active", "--quiet", SYSTEMD_TIMER],
        check=False,
    )
    return result.returncode == 0


def systemd_service_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_SERVICE


def systemd_timer_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / SYSTEMD_TIMER


def systemd_quote(path: Path) -> str:
    return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'


def autosync_executable() -> Path:
    executable = shutil.which("mm") or sys.argv[0]
    return Path(executable).expanduser().absolute()


def run_command(args: list[str], *, allowed_returncodes: set[int] | None = None) -> None:
    result = subprocess.run(args, check=False, capture_output=True, text=True)  # noqa: S603
    accepted = allowed_returncodes or {0}
    if result.returncode in accepted:
        return
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    message = f"Command failed: {' '.join(args)}: {detail}"
    raise RuntimeError(message)


def remove_autosync_files() -> list[Path]:
    candidates = [launchd_plist_path(), systemd_service_path(), systemd_timer_path()]
    removed: list[Path] = []
    for path in candidates:
        if not path.exists():
            continue
        path.unlink()
        removed.append(path)
    return removed
