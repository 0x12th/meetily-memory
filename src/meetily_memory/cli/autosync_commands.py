import sys
from pathlib import Path
from typing import Annotated

import typer

from meetily_memory.cli.common import make_typer, print_json, print_text_block
from meetily_memory.config.settings import load_app_settings, update_app_settings

autosync_app = make_typer("Manage automatic updates.")


@autosync_app.command("start")
def autosync_start(
    interval_minutes: Annotated[
        int,
        typer.Option("--interval-minutes", help="Background update interval."),
    ] = 30,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    settings = update_app_settings(autosync_enabled=True)
    launchd_plist = install_launchd_plist(interval_minutes) if sys.platform == "darwin" else None
    systemd_files = (
        install_systemd_user_units(interval_minutes) if sys.platform.startswith("linux") else []
    )
    payload = {
        "autosync_enabled": settings.autosync_enabled,
        "interval_minutes": interval_minutes,
        "launchd_plist": str(launchd_plist) if launchd_plist else None,
        "systemd_files": [str(path) for path in systemd_files],
    }
    if json_output:
        print_json(payload)
        return
    print_text_block("autosync: enabled")
    if launchd_plist:
        print_text_block(f"launchd plist: {launchd_plist}")
    for path in systemd_files:
        print_text_block(f"systemd file: {path}")


@autosync_app.command("stop")
def autosync_stop(
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    settings = update_app_settings(autosync_enabled=False)
    removed_files = remove_autosync_files()
    payload = {
        "autosync_enabled": settings.autosync_enabled,
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
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    settings = load_app_settings()
    payload = {
        "autosync_enabled": settings.autosync_enabled,
        "last_update_at": settings.last_update_at,
    }
    if json_output:
        print_json(payload)
        return
    print_text_block(f"autosync: {'enabled' if settings.autosync_enabled else 'disabled'}")
    print_text_block(f"last update: {settings.last_update_at or 'never'}")


def install_launchd_plist(interval_minutes: int) -> Path:
    label = "com.meetily-memory.autosync"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{label}.plist"
    plist_dir.mkdir(parents=True, exist_ok=True)
    seconds = max(interval_minutes, 1) * 60
    executable = Path(sys.argv[0]).resolve()
    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{executable}</string>
    <string>update</string>
  </array>
  <key>StartInterval</key>
  <integer>{seconds}</integer>
  <key>RunAtLoad</key>
  <true/>
</dict>
</plist>
"""
    plist_path.write_text(plist, encoding="utf-8")
    return plist_path


def install_systemd_user_units(interval_minutes: int) -> list[Path]:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    service_path = unit_dir / "meetily-memory-autosync.service"
    timer_path = unit_dir / "meetily-memory-autosync.timer"
    unit_dir.mkdir(parents=True, exist_ok=True)
    executable = Path(sys.argv[0]).resolve()
    minutes = max(interval_minutes, 1)
    service = f"""[Unit]
Description=Meetily Memory automatic update

[Service]
Type=oneshot
ExecStart={executable} update
"""
    timer = f"""[Unit]
Description=Run Meetily Memory automatic update

[Timer]
OnBootSec=1min
OnUnitActiveSec={minutes}min
Unit=meetily-memory-autosync.service

[Install]
WantedBy=timers.target
"""
    service_path.write_text(service, encoding="utf-8")
    timer_path.write_text(timer, encoding="utf-8")
    return [service_path, timer_path]


def remove_autosync_files() -> list[Path]:
    candidates = [
        Path.home() / "Library" / "LaunchAgents" / "com.meetily-memory.autosync.plist",
        Path.home() / ".config" / "systemd" / "user" / "meetily-memory-autosync.service",
        Path.home() / ".config" / "systemd" / "user" / "meetily-memory-autosync.timer",
    ]
    removed: list[Path] = []
    for path in candidates:
        if not path.exists():
            continue
        path.unlink()
        removed.append(path)
    return removed
