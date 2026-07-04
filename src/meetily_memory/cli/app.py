import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from meetily_memory.config.paths import default_index_path, discover_meetily_db
from meetily_memory.context_builder import DEFAULT_CONTEXT_LIMIT, build_context_markdown
from meetily_memory.db.repository import IndexRepository
from meetily_memory.json_codec import dumps_json
from meetily_memory.scanner.meetily_sqlite import MeetilySQLiteScanner
from meetily_memory.scanner.sqlite_source import can_open_readonly_sqlite
from meetily_memory.structure_analyzer import StructureAnalyzer

app = typer.Typer(no_args_is_help=True, help="Local Meetily history index.")
console = Console()
ENTITY_COMMANDS = {
    "decisions": "decisions",
    "tasks": "action_items",
    "risks": "risks",
    "questions": "open_questions",
}


def _index_option(
    index: Path | None,
) -> Path:
    return index or default_index_path()


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
) -> None:
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
) -> None:
    source_path = source or discover_meetily_db()
    if source_path is None:
        message = "Meetily DB was not found. Pass --source /path/to/meeting_minutes.sqlite."
        raise typer.BadParameter(message)
    result = MeetilySQLiteScanner(ctx.obj["index_path"]).scan(source_path, force=force)
    payload = {
        "source_id": result.source_id,
        "meetings_seen": result.meetings_seen,
        "meetings_inserted": result.meetings_inserted,
        "meetings_updated": result.meetings_updated,
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
    console.print(f"chunks seen: {result.chunks_seen}")


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
        console.print("No structured entities found. Run `mm analyze` after scanning.")
        return

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


@app.command("open")
def open_command(
    ctx: typer.Context,
    meeting_id: str,
    folder: Annotated[bool, typer.Option("--folder")] = False,
    print_path: Annotated[bool, typer.Option("--print-path")] = False,
) -> None:
    del folder
    repo = IndexRepository(ctx.obj["index_path"])
    meeting = repo.get_meeting(meeting_id)
    if not meeting:
        message = f"Meeting not found: {meeting_id}"
        raise typer.BadParameter(message)
    path = meeting.get("folder_path") or meeting.get("source_path")
    if not path:
        message = f"Meeting has no path: {meeting_id}"
        raise typer.BadParameter(message)
    if print_path:
        console.print(path)
        return
    open_path(Path(path))


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
