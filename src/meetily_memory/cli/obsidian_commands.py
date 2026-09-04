from pathlib import Path
from typing import Annotated

import typer

from meetily_memory.cli.common import make_typer, print_json, print_text_block
from meetily_memory.config.settings import load_app_settings
from meetily_memory.obsidian_notes import ObsidianSyncResult, obsidian_root_dir
from meetily_memory.obsidian_sync import sync_configured_obsidian_locked
from meetily_memory.refresh_lock import RefreshLock, RefreshLockBusyError
from meetily_memory.user_state import UserStateRepository

obsidian_app = make_typer("Sync Meetily Memory into Obsidian.")


def print_sync_result(result: ObsidianSyncResult) -> None:
    print_text_block(f"obsidian root: {result.root_dir}")
    print_text_block(f"obsidian files synced: {result.files_written}")
    print_text_block(f"obsidian files skipped: {result.files_skipped}")
    print_text_block(f"obsidian files removed: {result.files_removed}")


@obsidian_app.command("init")
def obsidian_init(
    ctx: typer.Context,
    vault: Annotated[
        Path,
        typer.Option("--vault", help="Obsidian vault path."),
    ] = Path("~/Documents/Obsidian"),
    folder: Annotated[
        str,
        typer.Option("--folder", help="Folder inside the vault."),
    ] = "Meetily Memory",
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    try:
        obsidian_root_dir(vault, folder)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    state_path = ctx.obj["state_path"]
    UserStateRepository(state_path).set_obsidian_target(str(vault.expanduser()), folder)
    try:
        with RefreshLock(ctx.obj["index_path"]):
            result = sync_configured_obsidian_locked(
                ctx.obj["index_path"],
                ctx.obj["state_path"],
                required=True,
            )
    except RefreshLockBusyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if result is None:
        message = "Configured Obsidian sync unexpectedly returned no result."
        raise RuntimeError(message)
    settings = load_app_settings(state_path)
    payload = {**settings.obsidian.__dict__, "sync": result.as_payload()}
    if json_output:
        print_json(payload)
        return
    print_text_block(f"obsidian vault: {settings.obsidian.vault_path}")
    print_text_block(f"obsidian folder: {settings.obsidian.folder}")
    print_sync_result(result)


@obsidian_app.command("sync")
def obsidian_sync(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    try:
        with RefreshLock(ctx.obj["index_path"]):
            result = sync_configured_obsidian_locked(
                ctx.obj["index_path"],
                ctx.obj["state_path"],
                required=True,
            )
    except RefreshLockBusyError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if result is None:
        message = "Configured Obsidian sync unexpectedly returned no result."
        raise RuntimeError(message)
    if json_output:
        print_json(result.as_payload())
        return
    print_sync_result(result)


@obsidian_app.command("status")
def obsidian_status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    settings = load_app_settings(ctx.obj["state_path"])
    payload = settings.obsidian.__dict__
    if json_output:
        print_json(payload)
        return
    if not settings.obsidian.vault_path:
        print_text_block("obsidian: not configured")
        return
    print_text_block("obsidian: configured")
    print_text_block(f"vault: {settings.obsidian.vault_path}")
    print_text_block(f"folder: {settings.obsidian.folder}")
    print_text_block(f"last sync: {settings.obsidian.last_sync_at or 'never'}")
