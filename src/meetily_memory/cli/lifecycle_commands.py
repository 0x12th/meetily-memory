from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, cast

import typer

from meetily_memory.cli.autosync_commands import autosync_runtime_status, enable_autosync
from meetily_memory.cli.common import (
    console,
    make_typer,
    print_json,
    print_text_block,
    resolve_ui_language,
    sqlite_has_fts5,
)
from meetily_memory.config.paths import discover_meetily_db
from meetily_memory.config.settings import AppSettings, load_app_settings, update_app_settings
from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION
from meetily_memory.db.repository import IndexRepository
from meetily_memory.db.schema import index_connection
from meetily_memory.integrations import sync_obsidian_vault
from meetily_memory.scanner.meetily_sqlite import (
    MeetilySQLiteScanner,
    inspect_meetily_schema,
    meeting_external_ids,
)
from meetily_memory.scanner.sqlite_source import can_open_readonly_sqlite
from meetily_memory.semantic_search import (
    index_semantic_embeddings,
    load_semantic_config,
    resolve_embedding_provider,
)
from meetily_memory.structure_analyzer import StructureAnalyzer

app = make_typer("Local Meetily history lifecycle commands.")
config_app = make_typer("Manage CLI settings.")
db_app = make_typer("Inspect the local index database.")
mcp_app = make_typer("Run the MCP server.")


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


SOURCE_KIND = "meetily_sqlite"


def migrated_source_settings(index_path: Path, settings_path: Path) -> AppSettings:
    settings = load_app_settings(settings_path)
    repo = IndexRepository(index_path)
    if settings.source_uuid:
        return settings
    if not settings.source_path:
        return settings
    source_uuid = repo.user_state.get_or_create_source(
        SOURCE_KIND,
        str(Path(settings.source_path).expanduser()),
        now=utc_now_iso(),
    )
    return update_app_settings(
        settings_path=settings_path,
        source_uuid=source_uuid,
        source_path=None,
    )


def configured_source_path(
    index_path: Path,
    settings_path: Path,
    explicit_source: Path | None = None,
) -> Path | None:
    if explicit_source is not None:
        return explicit_source
    settings = migrated_source_settings(index_path, settings_path)
    if settings.source_uuid:
        source = IndexRepository(index_path).user_state.get_source(settings.source_uuid)
        configured = Path(str(source["current_path"])).expanduser() if source else None
        if configured and configured.exists():
            return configured
    if settings.source_path:
        configured = Path(settings.source_path).expanduser()
        if configured.exists():
            return configured
    return discover_meetily_db()


def scan_update(
    index_path: Path,
    source_path: Path,
    *,
    semantic: bool = False,
) -> tuple[dict[str, object], int]:
    result = MeetilySQLiteScanner(index_path).scan(source_path, analyze=True)
    embeddings_indexed = 0

    if semantic:
        try:
            provider = resolve_embedding_provider()
            embeddings_indexed = index_semantic_embeddings(
                index_path,
                embedding_provider=provider,
            )
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc

    payload: dict[str, object] = {
        "source_id": result.source_id,
        "meetings_seen": result.meetings_seen,
        "meetings_inserted": result.meetings_inserted,
        "meetings_updated": result.meetings_updated,
        "meetings_analyzed": result.meetings_analyzed,
        "chunks_seen": result.chunks_seen,
        "chunks_inserted": result.chunks_inserted,
        "chunks_updated": result.chunks_updated,
        "embeddings_indexed": embeddings_indexed,
    }

    return payload, embeddings_indexed


def print_update_payload(payload: dict[str, object], *, semantic: bool) -> None:
    console.print(f"meetings seen: {payload['meetings_seen']}")
    console.print(f"meetings inserted: {payload['meetings_inserted']}")
    console.print(f"meetings updated: {payload['meetings_updated']}")
    console.print(f"meetings analyzed: {payload['meetings_analyzed']}")
    console.print(f"chunks seen: {payload['chunks_seen']}")
    if semantic:
        console.print(f"embeddings indexed: {payload['embeddings_indexed']}")


@app.command()
def init(
    ctx: typer.Context,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Path to Meetily meeting_minutes.sqlite."),
    ] = None,
    autosync: Annotated[
        bool | None,
        typer.Option("--autosync/--no-autosync", help="Enable automatic index refreshes."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    source_path = configured_source_path(ctx.obj["index_path"], ctx.obj["settings_path"], source)
    if source_path is None:
        message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
        raise typer.BadParameter(message)
    should_enable_autosync = autosync
    if should_enable_autosync is None:
        should_enable_autosync = typer.confirm("Enable automatic index refreshes?", default=False)
    payload, _ = scan_update(ctx.obj["index_path"], source_path)
    repo = IndexRepository(ctx.obj["index_path"])
    source_uuid = repo.user_state.get_or_create_source(
        SOURCE_KIND, str(source_path), now=utc_now_iso()
    )
    settings = update_app_settings(
        settings_path=ctx.obj["settings_path"],
        source_uuid=source_uuid,
        source_path=None,
        autosync_enabled=False,
        last_update_at=utc_now_iso(),
    )
    if should_enable_autosync:
        try:
            enable_autosync(
                ctx.obj["index_path"],
                ctx.obj["settings_path"],
                interval_minutes=30,
            )
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc
        settings = load_app_settings(ctx.obj["settings_path"])
    response = {
        "initialized": True,
        "index_path": str(ctx.obj["index_path"]),
        "source_path": str(source_path),
        "autosync_enabled": settings.autosync_enabled,
        **payload,
    }
    if json_output:
        print_json(response)
        return
    print_text_block("initialized: yes")
    print_text_block(f"index path: {ctx.obj['index_path']}")
    print_text_block(f"source path: {source_path}")
    print_text_block(f"autosync: {'enabled' if settings.autosync_enabled else 'disabled'}")
    print_update_payload(payload, semantic=False)


@app.command()
def status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    index_path = ctx.obj["index_path"]
    repo = IndexRepository(index_path)
    settings = migrated_source_settings(index_path, ctx.obj["settings_path"])
    autosync_status = autosync_runtime_status(ctx.obj["settings_path"], index_path)
    selected_source = (
        repo.user_state.get_source(settings.source_uuid) if settings.source_uuid else None
    )
    source_path = str(selected_source["current_path"]) if selected_source else None
    stats = repo.stats()
    semantic_config = load_semantic_config()
    obsidian_configured = bool(settings.obsidian.vault_path)
    llm_provider = settings.llm.provider or "not configured"
    resolved_ui_language = resolve_ui_language(index_path, ctx.obj["settings_path"])
    payload = {
        "index_path": str(index_path),
        "source_path": source_path,
        "ui_language": settings.ui_language,
        "resolved_ui_language": resolved_ui_language,
        "last_update_at": settings.last_update_at,
        "autosync_enabled": autosync_status.enabled,
        "autosync_configured": autosync_status.configured,
        "autosync_installed": autosync_status.installed,
        "autosync_active": autosync_status.active,
        "semantic_provider": semantic_config.provider,
        "obsidian_configured": obsidian_configured,
        "llm_provider": settings.llm.provider,
        **stats,
    }
    if json_output:
        print_json(payload)
        return
    print_text_block(f"index path: {index_path}")
    print_text_block(f"source path: {source_path or 'not configured'}")
    configured_label = "configured" if settings.ui_language else "auto"
    print_text_block(f"language: {resolved_ui_language} ({configured_label})")
    print_text_block(f"last refresh: {settings.last_update_at or 'never'}")
    print_text_block(f"autosync: {autosync_status.label}")
    print_text_block(f"semantic: {semantic_config.provider or 'not configured'}")
    print_text_block(f"obsidian: {'configured' if obsidian_configured else 'not configured'}")
    print_text_block(f"llm: {llm_provider}")
    print_text_block(f"meetings: {stats['meetings']}")
    print_text_block(f"chunks: {stats['chunks']}")


@config_app.command("language")
def config_language(
    ctx: typer.Context,
    language: Annotated[
        str,
        typer.Argument(help="UI language: en, ru, or auto."),
    ],
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    normalized = language.casefold().replace("_", "-").split("-", maxsplit=1)[0]
    if normalized == "auto":
        settings = update_app_settings(settings_path=ctx.obj["settings_path"], ui_language=None)
    elif normalized in {"en", "ru"}:
        settings = update_app_settings(
            settings_path=ctx.obj["settings_path"], ui_language=normalized
        )
    else:
        message = "UI language must be one of: en, ru, auto."
        raise typer.BadParameter(message)

    payload = {"ui_language": settings.ui_language}
    if json_output:
        print_json(payload)
        return
    print_text_block(f"ui language: {settings.ui_language or 'auto'}")


@config_app.command("source")
def config_source(
    ctx: typer.Context,
    new_path: Annotated[Path, typer.Argument(help="Path to Meetily meeting_minutes.sqlite.")],
    rebind: Annotated[
        bool,
        typer.Option("--rebind", help="Preserve the selected source UUID after verification."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    new_path = new_path.expanduser()
    valid, schema_error = inspect_meetily_schema(new_path)
    if not valid:
        raise typer.BadParameter(schema_error or "Meetily DB schema is unsupported.")
    index_path = ctx.obj["index_path"]
    repo = IndexRepository(index_path)
    settings = migrated_source_settings(index_path, ctx.obj["settings_path"])
    payload = (
        rebind_selected_source(repo, settings, new_path, ctx.obj["settings_path"])
        if rebind
        else select_source(repo, new_path, ctx.obj["settings_path"])
    )
    if json_output:
        print_json(payload)
        return
    if payload["rebound"]:
        print_text_block(f"old source path: {payload['old_source_path']}")
        print_text_block(f"new source path: {payload['new_source_path']}")
        print_text_block(f"matching meetings: {payload['matching_meetings']}")
        return
    print_text_block(f"source path: {payload['source_path']}")
    print_text_block(f"source uuid: {payload['source_uuid']}")


def select_source(repo: IndexRepository, new_path: Path, settings_path: Path) -> dict[str, object]:
    source_uuid = repo.user_state.get_or_create_source(
        SOURCE_KIND, str(new_path), now=utc_now_iso()
    )
    update_app_settings(
        settings_path=settings_path,
        source_uuid=source_uuid,
        source_path=None,
    )
    return {"source_uuid": source_uuid, "source_path": str(new_path), "rebound": False}


def rebind_selected_source(
    repo: IndexRepository, settings: AppSettings, new_path: Path, settings_path: Path
) -> dict[str, object]:
    if not settings.source_uuid:
        message = "No selected source is available to rebind."
        raise typer.BadParameter(message)
    current = repo.user_state.get_source(settings.source_uuid)
    if current is None:
        message = "The selected source no longer exists in user state."
        raise typer.BadParameter(message)
    old_path = str(current["current_path"])
    target = repo.user_state.get_source_by_path(SOURCE_KIND, str(new_path))
    if target and target["uuid"] != settings.source_uuid:
        message = "The new path is already linked to another source UUID."
        raise typer.BadParameter(message)
    indexed_ids = repo.source_meeting_external_ids(SOURCE_KIND, old_path)
    if not indexed_ids:
        return select_source(repo, new_path, settings_path)
    matching = indexed_ids & meeting_external_ids(new_path)
    if not matching:
        message = "The new source has no matching meeting IDs."
        raise typer.BadParameter(message)

    repo.user_state.update_source_path(settings.source_uuid, str(new_path), now=utc_now_iso())
    repo.update_source_path_projection(SOURCE_KIND, old_path, str(new_path))
    update_app_settings(
        settings_path=settings_path,
        source_uuid=settings.source_uuid,
        source_path=None,
    )
    return {
        "source_uuid": settings.source_uuid,
        "old_source_path": old_path,
        "new_source_path": str(new_path),
        "matching_meetings": len(matching),
        "rebound": True,
    }


@app.command()
def scan(
    ctx: typer.Context,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Path to Meetily meeting_minutes.sqlite."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
    force: Annotated[
        bool,
        typer.Option("--force", help="Reindex unchanged meetings and rebuild FTS rows."),
    ] = False,
    analyze_output: Annotated[
        bool,
        typer.Option("--analyze/--no-analyze", help="Analyze new or changed meetings."),
    ] = True,
) -> None:
    source_path = configured_source_path(ctx.obj["index_path"], ctx.obj["settings_path"], source)
    if source_path is None:
        message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
        raise typer.BadParameter(message)
    result = MeetilySQLiteScanner(ctx.obj["index_path"]).scan(
        source_path,
        force=force,
        analyze=analyze_output,
    )
    payload = {
        "source_id": result.source_id,
        "meetings_seen": result.meetings_seen,
        "meetings_inserted": result.meetings_inserted,
        "meetings_updated": result.meetings_updated,
        "meetings_analyzed": result.meetings_analyzed,
        "chunks_seen": result.chunks_seen,
        "chunks_inserted": result.chunks_inserted,
        "chunks_updated": result.chunks_updated,
    }
    if json_output:
        print_json(payload)
        return
    console.print(f"meetings seen: {result.meetings_seen}")
    console.print(f"meetings inserted: {result.meetings_inserted}")
    console.print(f"meetings updated: {result.meetings_updated}")
    console.print(f"meetings analyzed: {result.meetings_analyzed}")
    console.print(f"chunks seen: {result.chunks_seen}")


@app.command("refresh")
def refresh(
    ctx: typer.Context,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Path to Meetily meeting_minutes.sqlite."),
    ] = None,
    semantic: Annotated[
        bool,
        typer.Option("--semantic", help="Also refresh configured semantic embeddings."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    source_path = configured_source_path(ctx.obj["index_path"], ctx.obj["settings_path"], source)
    if source_path is None:
        message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
        raise typer.BadParameter(message)
    settings = load_app_settings(ctx.obj["settings_path"])
    run_semantic = semantic or bool(load_semantic_config().provider)
    payload, _ = scan_update(ctx.obj["index_path"], source_path, semantic=run_semantic)
    repo = IndexRepository(ctx.obj["index_path"])
    source_uuid = repo.user_state.get_or_create_source(
        SOURCE_KIND, str(source_path), now=utc_now_iso()
    )
    settings = update_app_settings(
        settings_path=ctx.obj["settings_path"],
        source_uuid=source_uuid,
        source_path=None,
        last_update_at=utc_now_iso(),
    )
    obsidian_synced = False
    if settings.obsidian.vault_path and settings.obsidian.sync_after_update:
        sync_obsidian_vault(
            ctx.obj["index_path"],
            Path(settings.obsidian.vault_path),
            settings.obsidian.folder,
        )
        obsidian_synced = True
    if json_output:
        payload["obsidian_synced"] = obsidian_synced
        print_json(payload)
        return
    print_update_payload(payload, semantic=run_semantic)
    if obsidian_synced:
        console.print("obsidian sync: yes")


@app.command("update")
def update() -> None:
    """Update the installed meetily-memory utility through Homebrew."""
    brew = shutil.which("brew")
    if brew is None:
        message = (
            "Homebrew was not found. If Meetily Memory was installed another way, "
            "update it with that package manager."
        )
        raise typer.BadParameter(message)
    result = subprocess.run([brew, "upgrade", "meetily-memory"], check=False)  # noqa: S603
    if result.returncode != 0:
        message = "Homebrew upgrade failed: brew upgrade meetily-memory"
        raise typer.BadParameter(message)
    print_text_block("updated: meetily-memory")


@db_app.command("status")
def db_status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    index_path = ctx.obj["index_path"]
    repo = IndexRepository(index_path)
    with index_connection(index_path) as conn:
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    migration_report = repo.user_state.latest_migration_report()
    payload = {
        "index_path": str(index_path),
        "state_path": str(repo.state_path),
        "schema_version": schema_version,
        "current_schema_version": CURRENT_SCHEMA_VERSION,
        "user_state_migration": migration_report,
    }
    if json_output:
        print_json(payload)
        return
    print_text_block(f"index path: {index_path}")
    print_text_block(f"state path: {repo.state_path}")
    print_text_block(f"schema version: {schema_version}")
    print_text_block(f"current schema version: {CURRENT_SCHEMA_VERSION}")
    if migration_report:
        print_text_block(
            "user state migration: "
            f"{migration_report['migrated']} migrated, "
            f"{migration_report['orphaned']} orphaned"
        )


@mcp_app.command("serve")
def mcp_serve(
    ctx: typer.Context,
    transport: Annotated[
        str,
        typer.Option("--transport", help="MCP transport: stdio, sse, or streamable-http."),
    ] = "stdio",
) -> None:
    if transport not in {"stdio", "sse", "streamable-http"}:
        message = "MCP transport must be one of: stdio, sse, streamable-http."
        raise typer.BadParameter(message)
    try:
        from meetily_memory.mcp_server import MCPTransport, run_mcp_server  # noqa: PLC0415
    except ImportError as exc:
        message = "MCP support is optional. Install with `meetily-memory[mcp]`."
        raise typer.BadParameter(message) from exc
    run_mcp_server(ctx.obj["index_path"], transport=cast("MCPTransport", transport))


@app.command()
def analyze(
    ctx: typer.Context,
    meeting_id: Annotated[
        str | None,
        typer.Argument(help="Meeting external id or internal id. Omit to analyze all meetings."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    analyzer = StructureAnalyzer(repo)
    if meeting_id:
        meeting = repo.get_meeting(meeting_id)
        if not meeting:
            message = f"Meeting not found: {meeting_id}"
            raise typer.BadParameter(message)
        result = analyzer.analyze_meeting(int(meeting["id"]))
    else:
        result = analyzer.analyze_all()

    payload = result.as_payload()
    if json_output:
        print_json(payload)
        return
    console.print(f"meetings analyzed: {payload['meetings_analyzed']}")
    console.print(f"decisions: {payload['decisions']}")
    console.print(f"action items: {payload['action_items']}")
    console.print(f"risks: {payload['risks']}")
    console.print(f"open questions: {payload['open_questions']}")


@app.command()
def doctor(
    ctx: typer.Context,
    source: Annotated[Path | None, typer.Option("--source")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    index_path = ctx.obj["index_path"]
    repo = IndexRepository(index_path)
    source_path = source or discover_meetily_db()
    source_readable = can_open_readonly_sqlite(source_path) if source_path else False
    source_schema_valid = False
    source_schema_error = None
    if source_path and source_readable:
        source_schema_valid, source_schema_error = inspect_meetily_schema(source_path)
    fts5 = sqlite_has_fts5()
    stats = repo.stats()
    payload = {
        "index_path": str(index_path),
        "source_path": str(source_path) if source_path else None,
        "source_readable": source_readable,
        "source_schema_valid": source_schema_valid,
        "source_schema_error": source_schema_error,
        "fts5": fts5,
        **stats,
    }
    if json_output:
        print_json(payload)
        return
    console.print(f"index path: {index_path}")
    console.print(f"source path: {source_path or 'not found'}")
    console.print(f"source readable: {'yes' if source_readable else 'no'}")
    console.print(f"source schema: {'valid' if source_schema_valid else 'invalid'}")
    if source_schema_error:
        console.print(f"source schema error: {source_schema_error}")
    console.print(f"fts5: {'yes' if fts5 else 'no'}")
    console.print(f"meetings: {stats['meetings']}")
    console.print(f"chunks: {stats['chunks']}")
    console.print(f"decisions: {stats['decisions']}")
    console.print(f"action items: {stats['action_items']}")
    console.print(f"risks: {stats['risks']}")
    console.print(f"open questions: {stats['open_questions']}")
