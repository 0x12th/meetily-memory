import sqlite3
import subprocess
import sys
from contextlib import closing
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from meetily_memory import __version__ as fallback_version
from meetily_memory.config.paths import default_index_path, discover_meetily_db
from meetily_memory.context_builder import DEFAULT_CONTEXT_LIMIT, build_context_markdown
from meetily_memory.db.migrations import CURRENT_SCHEMA_VERSION
from meetily_memory.db.repository import IndexRepository
from meetily_memory.db.schema import index_connection
from meetily_memory.json_codec import dumps_json
from meetily_memory.local_memory import (
    person_memory as build_person_memory,
)
from meetily_memory.local_memory import (
    project_memory as build_project_memory,
)
from meetily_memory.local_memory import (
    summary_memory,
    timeline_signals,
)
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.scanner.sqlite_source import can_open_readonly_sqlite
from meetily_memory.semantic_search import (
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_URL,
    SemanticSearchConfig,
    index_semantic_embeddings,
    load_semantic_config,
    resolve_embedding_provider,
    save_semantic_config,
    semantic_search,
)
from meetily_memory.spotlight_export import (
    clean_spotlight_markdown,
    export_spotlight_markdown,
)
from meetily_memory.structure_analyzer import StructureAnalyzer

PACKAGE_NAME = "meetily-memory"

app = typer.Typer(
    no_args_is_help=True,
    help="Local Meetily history index.",
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
    suggest_commands=False,
)
semantic_app = typer.Typer(
    no_args_is_help=True,
    help="Experimental semantic search commands.",
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
    suggest_commands=False,
)
spotlight_app = typer.Typer(
    no_args_is_help=True,
    help="Export Spotlight-friendly files.",
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
    suggest_commands=False,
)
db_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect the local index database.",
    add_completion=False,
    pretty_exceptions_enable=False,
    rich_markup_mode=None,
    suggest_commands=False,
)
app.add_typer(semantic_app, name="semantic")
app.add_typer(spotlight_app, name="spotlight")
app.add_typer(db_app, name="db")
console = Console()
ENTITY_COMMANDS = {
    "decisions": "decisions",
    "tasks": "action_items",
    "risks": "risks",
    "questions": "open_questions",
}
ENTITY_LABELS = {
    "decisions": "Decisions",
    "action_items": "Action items",
    "risks": "Risks",
    "open_questions": "Open questions",
}


def _index_option(
    index: Path | None,
) -> Path:
    return index or default_index_path()


def package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return fallback_version


def version_callback(value: bool) -> None:  # noqa: FBT001
    if value:
        print_text_block(f"{PACKAGE_NAME} {package_version()}")
        raise typer.Exit


def print_json(payload: object) -> None:
    sys.stdout.write(dumps_json(payload))
    sys.stdout.write("\n")


def print_text_block(text: str) -> None:
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def meeting_label(row: dict[str, object]) -> str:
    date = compact_date(row.get("updated_at") or row.get("created_at"))
    suffix = f" ({date})" if date else ""
    return f"#{row['id']} {row['title']}{suffix}"


def print_meeting_table(rows: list[dict[str, object]]) -> None:
    table = Table("id", "date", "chunks", "open", "title")
    for row in rows:
        table.add_row(
            str(row["id"]),
            compact_date(row.get("updated_at") or row.get("created_at")),
            str(row["chunk_count"]),
            f"mm open {row['id']}",
            str(row["title"]),
        )
    console.print(table)


def compact_date(value: object) -> str:
    return str(value or "")[:10]


@app.callback()
def callback(
    ctx: typer.Context,
    index: Annotated[
        Path | None,
        typer.Option("--index", help="Path to Meetily Memory index.sqlite."),
    ] = None,
    version_output: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=version_callback,
            help="Show version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    del version_output
    ctx.obj = {"index_path": _index_option(index)}


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
    source_path = source or discover_meetily_db()
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


@app.command()
def update(
    ctx: typer.Context,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Path to Meetily meeting_minutes.sqlite."),
    ] = None,
    semantic: Annotated[
        bool,
        typer.Option("--semantic", help="Also update configured semantic embeddings."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    source_path = source or discover_meetily_db()
    if source_path is None:
        message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
        raise typer.BadParameter(message)
    result = MeetilySQLiteScanner(ctx.obj["index_path"]).scan(source_path, analyze=True)
    embeddings_indexed = 0
    if semantic:
        try:
            provider = resolve_embedding_provider()
            embeddings_indexed = index_semantic_embeddings(
                ctx.obj["index_path"],
                embedding_provider=provider,
            )
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc
    payload = {
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
    if json_output:
        print_json(payload)
        return
    console.print(f"meetings seen: {result.meetings_seen}")
    console.print(f"meetings inserted: {result.meetings_inserted}")
    console.print(f"meetings updated: {result.meetings_updated}")
    console.print(f"meetings analyzed: {result.meetings_analyzed}")
    console.print(f"chunks seen: {result.chunks_seen}")
    if semantic:
        console.print(f"embeddings indexed: {embeddings_indexed}")


@db_app.command("status")
def db_status(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    index_path = ctx.obj["index_path"]
    with index_connection(index_path) as conn:
        schema_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    payload = {
        "index_path": str(index_path),
        "schema_version": schema_version,
        "current_schema_version": CURRENT_SCHEMA_VERSION,
    }
    if json_output:
        print_json(payload)
        return
    print_text_block(f"index path: {index_path}")
    print_text_block(f"schema version: {schema_version}")
    print_text_block(f"current schema version: {CURRENT_SCHEMA_VERSION}")


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


@app.command("s")
def search(
    ctx: typer.Context,
    query: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    results = repo.search(query, limit)
    if json_output:
        print_json(results)
        return
    for result in results:
        date = compact_date(result.get("updated_at") or result.get("created_at"))
        suffix = f" ({date})" if date else ""
        console.print(f"#{result['meeting_id']} {result['title']}{suffix}")
        source_parts = [
            f"chunk #{result['chunk_id']}",
            f"open: mm open {result['meeting_id']}",
        ]
        if result.get("timestamp_label"):
            source_parts.insert(0, str(result["timestamp_label"]))
        console.print(" | ".join(source_parts))
        console.print(result["text"])
        console.print()


def semantic_search_command(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Search query.")],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    embedding_provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            "--embedding-provider",
            help="Embedding provider: ollama or hash.",
        ),
    ] = None,
    embedding_model: Annotated[
        str | None,
        typer.Option("--model", "--embedding-model", help="Ollama embedding model name."),
    ] = None,
    ollama_url: Annotated[
        str | None,
        typer.Option("--ollama-url", help="Ollama base URL."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        provider = resolve_embedding_provider(
            embedding_provider,
            ollama_model=embedding_model,
            ollama_url=ollama_url,
        )
        results = semantic_search(ctx.obj["index_path"], query, limit, embedding_provider=provider)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        print_json(results)
        return
    if not results:
        console.print("No semantic matches found. Run `mm scan` first.")
        return
    for result in results:
        date = compact_date(result.get("updated_at") or result.get("created_at"))
        suffix = f" ({date})" if date else ""
        console.print(f"#{result['meeting_id']} {result['title']}{suffix}")
        distance = float_value(result["distance"], "semantic distance")
        console.print(
            f"semantic distance: {distance:.4f} | "
            f"embedding: {embedding_label(result, provider)} | "
            f"open: mm open {result['meeting_id']}"
        )
        source_parts = [f"chunk #{result['chunk_id']}"]
        if result.get("timestamp_label"):
            source_parts.insert(0, str(result["timestamp_label"]))
        console.print(" | ".join(source_parts))
        console.print(result["text"])
        console.print()


semantic_app.command("search")(semantic_search_command)
app.command("sem")(semantic_search_command)


@semantic_app.command("setup")
def semantic_setup_command(
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            "--embedding-provider",
            help="Embedding provider: ollama or hash.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", "--embedding-model", help="Ollama embedding model name."),
    ] = None,
    ollama_url: Annotated[
        str | None,
        typer.Option("--ollama-url", help="Ollama base URL."),
    ] = None,
    show: Annotated[
        bool,
        typer.Option("--show", help="Show semantic search setup."),
    ] = False,
) -> None:
    semantic_setup(provider, model, ollama_url, show=show)


@semantic_app.command("index")
def semantic_index_command(
    ctx: typer.Context,
    embedding_provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            "--embedding-provider",
            help="Embedding provider: ollama or hash.",
        ),
    ] = None,
    embedding_model: Annotated[
        str | None,
        typer.Option("--model", "--embedding-model", help="Ollama embedding model name."),
    ] = None,
    ollama_url: Annotated[
        str | None,
        typer.Option("--ollama-url", help="Ollama base URL."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        provider = resolve_embedding_provider(
            embedding_provider,
            ollama_model=embedding_model,
            ollama_url=ollama_url,
        )
        indexed = index_semantic_embeddings(ctx.obj["index_path"], embedding_provider=provider)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {
        "embedding_provider": provider.name,
        "embedding_model": provider.model,
        "embeddings_indexed": indexed,
    }
    if json_output:
        print_json(payload)
        return
    print_text_block(f"embedding: {provider.name}/{provider.model}")
    print_text_block(f"embeddings indexed: {indexed}")


def semantic_setup(
    provider: str | None,
    model: str | None,
    ollama_url: str | None,
    *,
    show: bool,
) -> None:
    existing = load_semantic_config()
    if show and provider is None and model is None and ollama_url is None:
        print_semantic_config(existing)
        return

    normalized_provider = normalize_semantic_provider(provider or existing.provider or "ollama")
    existing_model = existing.ollama_model if existing.provider == normalized_provider else None
    existing_ollama_url = existing.ollama_url if existing.provider == normalized_provider else None
    configured_ollama_url = None
    if normalized_provider == "ollama":
        configured_ollama_url = ollama_url or existing_ollama_url or DEFAULT_OLLAMA_URL
    config = SemanticSearchConfig(
        provider=normalized_provider,
        ollama_model=model or existing_model or default_model_for_provider(normalized_provider),
        ollama_url=configured_ollama_url,
    )
    config_path = save_semantic_config(config)
    print_semantic_config(config)
    print_text_block(f"config path: {config_path}")


def normalize_semantic_provider(provider: str) -> str:
    value = provider.casefold()
    if value in {"hash", "local-hash", "diagnostic"}:
        return "hash"
    if value == "ollama":
        return "ollama"
    message = "Unknown embedding provider. Use `ollama` or `hash`."
    raise typer.BadParameter(message)


def default_model_for_provider(provider: str) -> str:
    if provider == "hash":
        return "local-hash-v1"
    return DEFAULT_OLLAMA_MODEL


def print_semantic_config(config: SemanticSearchConfig) -> None:
    provider = config.provider or "ollama"
    model = config.ollama_model or default_model_for_provider(provider)
    ollama_url = config.ollama_url or DEFAULT_OLLAMA_URL
    print_text_block(f"semantic provider: {provider}")
    print_text_block(f"model: {model}")
    if provider == "ollama":
        print_text_block(f"ollama url: {ollama_url}")


@app.command("c")
def context(
    ctx: typer.Context,
    question: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = DEFAULT_CONTEXT_LIMIT,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    results = repo.search(question, limit)
    print_text_block(build_context_markdown(question, results))


@app.command("ls")
def list_meetings(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    rows = repo.list_meetings(limit)
    if json_output:
        print_json(rows)
        return
    print_meeting_table(rows)


@app.command()
def last(
    ctx: typer.Context,
    person: Annotated[str | None, typer.Option("--person")] = None,
    transcript: Annotated[bool, typer.Option("--transcript")] = False,
    summary: Annotated[bool, typer.Option("--summary")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    rows = repo.list_meetings(limit=1, person=person)
    if not rows:
        raise typer.Exit(1)
    meeting = rows[0]
    if json_output:
        print_json(meeting)
        return
    console.print(meeting_label(meeting))
    console.print(f"open: mm open {meeting['id']}")
    if summary and meeting.get("summary_text"):
        console.print(meeting["summary_text"])
    if transcript:
        chunks = repo.get_chunks_for_meeting(meeting["id"])
        for chunk in chunks:
            if chunk["kind"] == "transcript":
                console.print(chunk["text"])


@app.command("p")
def person(
    ctx: typer.Context,
    name: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    rows = repo.list_meetings(limit=limit, person=name)
    if json_output:
        print_json(rows)
        return
    print_meeting_table(rows)


@app.command("summary")
def local_summary(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    memory = summary_memory(repo)
    if json_output:
        print_json(memory.as_payload())
        return
    stats = memory.stats
    print_text_block("Local memory summary")
    print_text_block(f"meetings: {stats['meetings']}")
    print_text_block(f"chunks: {stats['chunks']}")
    if memory.latest_meeting:
        print_text_block(f"latest meeting: {meeting_label(memory.latest_meeting)}")
    print_text_block(f"decisions: {stats['decisions']}")
    print_text_block(f"action items: {stats['action_items']}")
    print_text_block(f"risks: {stats['risks']}")
    print_text_block(f"open questions: {stats['open_questions']}")


@app.command("timeline")
def timeline(
    ctx: typer.Context,
    query: Annotated[str | None, typer.Argument(help="Optional project/topic filter.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    rows = timeline_signals(repo, query, limit)
    if json_output:
        print_json(rows)
        return
    if not rows:
        console.print("No timeline signals found. Run `mm analyze` after scanning.")
        return
    for row in rows:
        print_entity_timeline_row(row)


@app.command("project")
def project_memory(
    ctx: typer.Context,
    query: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    memory = build_project_memory(repo, query, limit)
    if json_output:
        print_json(memory.as_payload())
        return
    print_text_block(f"Project memory: {query}")
    print_text_block("\nMeetings")
    print_search_meeting_summaries(memory.meetings)
    print_text_block("Structured signals")
    print_entity_bullets(memory.structured_signals)


@app.command("person")
def person_memory(
    ctx: typer.Context,
    name: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    memory = build_person_memory(repo, name, limit)
    if json_output:
        print_json(memory.as_payload())
        return
    print_text_block(f"Person memory: {name}")
    print_text_block("\nLatest meetings")
    print_meeting_summaries(memory.meetings)
    print_grouped_entity_bullets(memory.structured_signals)


@app.command("decisions")
def decisions(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    print_structured_entities(ctx, ENTITY_COMMANDS["decisions"], limit, json_output=json_output)


@app.command("tasks")
def tasks(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    print_structured_entities(ctx, ENTITY_COMMANDS["tasks"], limit, json_output=json_output)


@app.command("risks")
def risks(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    print_structured_entities(ctx, ENTITY_COMMANDS["risks"], limit, json_output=json_output)


@app.command("questions")
def questions(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    print_structured_entities(ctx, ENTITY_COMMANDS["questions"], limit, json_output=json_output)


def print_structured_entities(
    ctx: typer.Context,
    kind: str,
    limit: int,
    *,
    json_output: bool,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    rows = repo.list_structured_entity_details(kind, limit)
    if json_output:
        print_json(rows)
        return
    if not rows:
        console.print("No heuristic structured signals found. Run `mm analyze` after scanning.")
        return

    print_text_block(f"Heuristic {ENTITY_LABELS[kind].casefold()}")
    for row in rows:
        source_parts = [
            str(row["meeting_external_id"]),
            str(row.get("chunk_external_id") or row.get("source_chunk_id") or ""),
        ]
        source = " / ".join(part for part in source_parts if part)
        if row.get("chunk_timestamp_label"):
            source = f"{source} @ {row['chunk_timestamp_label']}"
        print_text_block(f"{row['meeting_title']} [{row['meeting_external_id']}]")
        print_text_block(f"Date: {row.get('meeting_date') or ''}")
        print_text_block(f"Source: {source}")
        print_text_block(f"confidence: {float(row['confidence']):.2f}")
        print_text_block(str(row["text"]))
        sys.stdout.write("\n")


def print_entity_timeline_row(row: dict[str, object]) -> None:
    print_text_block(f"{row.get('meeting_date') or ''} | {row['meeting_title']}")
    print_text_block(str(ENTITY_LABELS.get(str(row["kind"]), row["kind"])))
    print_text_block(f"Source: {entity_source(row)}")
    print_text_block(str(row["text"]))
    sys.stdout.write("\n")


def print_search_meeting_summaries(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No matching meetings.")
        return
    seen: set[int] = set()
    for row in rows:
        meeting_id = int(str(row["meeting_id"]))
        if meeting_id in seen:
            continue
        seen.add(meeting_id)
        date = compact_date(row.get("updated_at") or row.get("created_at"))
        suffix = f" ({date})" if date else ""
        print_text_block(f"- #{meeting_id} {row['title']}{suffix} | open: mm open {meeting_id}")


def print_meeting_summaries(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No matching meetings.")
        return
    for row in rows:
        print_text_block(f"- {meeting_label(row)} | open: mm open {row['id']}")


def print_entity_bullets(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No structured signals.")
        return
    for row in rows:
        label = ENTITY_LABELS.get(str(row["kind"]), str(row["kind"]))
        print_text_block(f"- {label}: {row['text']} ({entity_source(row)})")


def print_grouped_entity_bullets(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No structured signals.")
        return
    for kind in ENTITY_COMMANDS.values():
        kind_rows = [row for row in rows if row["kind"] == kind]
        if not kind_rows:
            continue
        print_text_block(f"\n{ENTITY_LABELS[kind]}")
        print_entity_bullets(kind_rows)


def entity_source(row: dict[str, object]) -> str:
    source_parts = [
        str(row.get("meeting_external_id") or row.get("meeting_id") or ""),
        str(row.get("chunk_external_id") or row.get("source_chunk_id") or ""),
    ]
    source = " / ".join(part for part in source_parts if part)
    if row.get("chunk_timestamp_label"):
        return f"{source} @ {row['chunk_timestamp_label']}"
    return source


def embedding_label(row: dict[str, object], provider: object) -> str:
    provider_name = str(row.get("embedding_provider") or getattr(provider, "name", ""))
    model = str(row.get("embedding_model") or getattr(provider, "model", ""))
    dimensions = row.get("embedding_dimensions") or getattr(provider, "dims", None)
    suffix = f"/{dimensions}d" if dimensions else ""
    return f"{provider_name}/{model}{suffix}"


def float_value(value: object, label: str) -> float:
    if isinstance(value, int | float):
        return float(value)
    message = f"Expected numeric {label}."
    raise RuntimeError(message)


@app.command("open")
def open_command(
    ctx: typer.Context,
    meeting_id: str,
    folder: Annotated[bool, typer.Option("--folder")] = False,
    print_path: Annotated[bool, typer.Option("--print-path")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    meeting = repo.get_meeting(meeting_id)
    if not meeting:
        message = f"Meeting not found: {meeting_id}"
        raise typer.BadParameter(message)
    path = meeting.get("folder_path") if folder else meeting.get("source_path")
    path = path or meeting.get("folder_path") or meeting.get("source_path")
    if not path:
        message = f"Meeting has no path: {meeting_id}"
        raise typer.BadParameter(message)
    if print_path:
        print_text_block(str(path))
        return
    open_path(Path(path))


@spotlight_app.command("export")
def spotlight_export(
    ctx: typer.Context,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Directory for Spotlight Markdown files."),
    ] = None,
    transcript: Annotated[
        bool,
        typer.Option("--transcript/--no-transcript", help="Include transcript text."),
    ] = True,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    repo = IndexRepository(ctx.obj["index_path"])
    result = export_spotlight_markdown(repo, output, include_transcript=transcript)
    if json_output:
        print_json(result.as_payload())
        return
    console.print(f"spotlight path: {result.output_dir}")
    console.print(f"meetings exported: {result.meetings_exported}")
    console.print(f"stale files removed: {result.files_removed}")


@spotlight_app.command("clean")
def spotlight_clean(
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Directory for Spotlight Markdown files."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    result = clean_spotlight_markdown(output)
    if json_output:
        print_json(result.as_payload())
        return
    console.print(f"spotlight path: {result.output_dir}")
    console.print(f"files removed: {result.files_removed}")


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
    fts5 = sqlite_has_fts5()
    stats = repo.stats()
    payload = {
        "index_path": str(index_path),
        "source_path": str(source_path) if source_path else None,
        "source_readable": source_readable,
        "fts5": fts5,
        **stats,
    }
    if json_output:
        print_json(payload)
        return
    console.print(f"index path: {index_path}")
    console.print(f"source path: {source_path or 'not found'}")
    console.print(f"source readable: {'yes' if source_readable else 'no'}")
    console.print(f"fts5: {'yes' if fts5 else 'no'}")
    console.print(f"meetings: {stats['meetings']}")
    console.print(f"chunks: {stats['chunks']}")
    console.print(f"decisions: {stats['decisions']}")
    console.print(f"action items: {stats['action_items']}")
    console.print(f"risks: {stats['risks']}")
    console.print(f"open questions: {stats['open_questions']}")


def sqlite_has_fts5() -> bool:
    with closing(sqlite3.connect(":memory:")) as conn:
        try:
            conn.execute("CREATE VIRTUAL TABLE fts_test USING fts5(value)")
        except sqlite3.Error:
            return False
        else:
            return True


def open_path(path: Path) -> None:
    if sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    elif sys.platform.startswith("win"):
        subprocess.run(["explorer", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


def main() -> None:
    app()
