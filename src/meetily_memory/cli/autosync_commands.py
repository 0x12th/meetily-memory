from pathlib import Path
from typing import Annotated

import typer

from meetily_memory.autosync import (
    AutosyncError,
    AutosyncStatus,
    disable,
    enable,
    status,
)
from meetily_memory.cli.common import make_typer, print_json, print_text_block
from meetily_memory.config.settings import load_app_settings

autosync_app = make_typer("Manage periodic index refresh with macOS launchd.")


def print_autosync_status(result: AutosyncStatus, settings_path: Path) -> None:
    settings = load_app_settings(settings_path)
    payload = result.as_payload()
    print_text_block(f"autosync: {result.state}")
    print_text_block("scheduler: launchd")
    print_text_block("schedule: :00, :15, :30, :45")
    print_text_block(f"configured index: {payload['configured_index'] or 'none'}")
    print_text_block(f"current index: {result.current_index}")
    print_text_block(f"loaded: {'yes' if result.loaded else 'no'}")
    last_exit_code = result.last_exit_code if result.last_exit_code is not None else "unknown"
    print_text_block(f"last exit code: {last_exit_code}")
    print_text_block(f"stderr log: {payload['stderr_log'] or 'none'}")
    print_text_block(f"last refresh: {settings.last_update_at or 'never'}")
    print_text_block(f"last obsidian sync: {settings.obsidian.last_sync_at or 'never'}")


@autosync_app.command("enable")
def autosync_enable(
    ctx: typer.Context,
    replace: Annotated[
        bool,
        typer.Option("--replace", help="Replace autosync for another workspace."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    try:
        result = enable(ctx.obj["index_path"], replace=replace)
    except AutosyncError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        print_json(result.as_payload())
        return
    print_autosync_status(result, ctx.obj["settings_path"])


@autosync_app.command("disable")
def autosync_disable(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    try:
        result = disable(ctx.obj["index_path"])
    except AutosyncError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        print_json(result.as_payload())
        return
    print_autosync_status(result, ctx.obj["settings_path"])


@autosync_app.command("status")
def autosync_status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    try:
        result = status(ctx.obj["index_path"])
    except AutosyncError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        print_json(result.as_payload())
        return
    print_autosync_status(result, ctx.obj["settings_path"])
