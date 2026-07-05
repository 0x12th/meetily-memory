from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

from meetily_memory.cli.common import make_typer, print_json, print_text_block
from meetily_memory.config.settings import ObsidianSettings, load_app_settings, update_app_settings
from meetily_memory.integrations import sync_obsidian_vault

obsidian_app = make_typer("Sync Meetily Memory into Obsidian.")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@obsidian_app.command("init")
def obsidian_init(
    vault: Annotated[
        Path,
        typer.Option("--vault", help="Obsidian vault path."),
    ] = Path("~/Documents/Obsidian"),
    folder: Annotated[
        str,
        typer.Option("--folder", help="Folder inside the vault."),
    ] = "Meetily Memory",
    sync_after_update: Annotated[
        bool,
        typer.Option(
            "--sync-after-refresh/--no-sync-after-refresh",
            help="Run Obsidian sync after mm refresh.",
        ),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    settings = update_app_settings(
        obsidian=ObsidianSettings(
            vault_path=str(vault.expanduser()),
            folder=folder,
            sync_after_update=sync_after_update,
        )
    )
    payload = settings.obsidian.__dict__
    if json_output:
        print_json(payload)
        return
    print_text_block(f"obsidian vault: {settings.obsidian.vault_path}")
    print_text_block(f"obsidian folder: {settings.obsidian.folder}")
    sync_after_refresh = "yes" if settings.obsidian.sync_after_update else "no"
    print_text_block(f"sync after refresh: {sync_after_refresh}")


@obsidian_app.command("sync")
def obsidian_sync(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    settings = load_app_settings()
    if not settings.obsidian.vault_path:
        message = "Obsidian is not configured. Run `mm obsidian init`."
        raise typer.BadParameter(message)
    result = sync_obsidian_vault(
        ctx.obj["index_path"],
        Path(settings.obsidian.vault_path),
        settings.obsidian.folder,
    )
    updated_obsidian = ObsidianSettings(
        vault_path=settings.obsidian.vault_path,
        folder=settings.obsidian.folder,
        sync_after_update=settings.obsidian.sync_after_update,
        last_sync_at=utc_now_iso(),
    )
    update_app_settings(obsidian=updated_obsidian)
    if json_output:
        print_json(result.as_payload())
        return
    print_text_block(f"obsidian root: {result.root_dir}")
    print_text_block(f"obsidian files synced: {result.files_written}")
    print_text_block(f"obsidian files skipped: {result.files_skipped}")


@obsidian_app.command("status")
def obsidian_status(
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    settings = load_app_settings()
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
    sync_after_refresh = "yes" if settings.obsidian.sync_after_update else "no"
    print_text_block(f"sync after refresh: {sync_after_refresh}")
    print_text_block(f"last sync: {settings.obsidian.last_sync_at or 'never'}")
