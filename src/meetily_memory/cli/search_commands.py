from pathlib import Path
from typing import Annotated, cast

import typer

from meetily_memory.cli.common import (
    compact_date,
    console,
    core_from_context,
    make_typer,
    open_path,
    print_json,
    print_text_block,
    ui_language_from_context,
)
from meetily_memory.cli.renderers import print_topic_memory
from meetily_memory.context_builder import DEFAULT_CONTEXT_LIMIT
from meetily_memory.db.repository import IndexRepository

app = make_typer("Search and context commands.")


@app.command("s")
def search(
    ctx: typer.Context,
    query: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    context: Annotated[
        int,
        typer.Option("--context", "-C", min=0, help="Include N chunks before and after each hit."),
    ] = 0,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    results = core_from_context(ctx).search(query, limit, context).data["results"]
    if json_output:
        print_json(results)
        return
    print_search_results(cast("list[dict[str, object]]", results))


def print_search_results(results: list[dict[str, object]]) -> None:
    grouped: dict[int, list[dict[str, object]]] = {}
    for result in results:
        meeting_id = int(cast("int | str", result["meeting_id"]))
        grouped.setdefault(meeting_id, []).append(result)
    for index, rows in enumerate(grouped.values()):
        if index:
            console.print()
        print_search_meeting_header(rows[0])
        for result in rows:
            print_search_excerpt(result)


def print_search_meeting_header(result: dict[str, object]) -> None:
    meeting_id = result["meeting_id"]
    date = compact_date(result.get("updated_at") or result.get("created_at"))
    suffix = f" ({date})" if date else ""
    console.print(f"#{meeting_id} {result['title']}{suffix}")
    console.print(f"open: mm open {meeting_id}")


def print_search_excerpt(result: dict[str, object]) -> None:
    source_parts = [
        f"chunk #{result['chunk_id']}",
    ]
    if result.get("timestamp_label"):
        source_parts.insert(0, str(result["timestamp_label"]))
    if result.get("is_context"):
        source_parts.append("context")
    console.print(" | ".join(source_parts))
    text = str(result["text"])
    if result.get("speaker"):
        text = f"{result['speaker']}: {text}"
    console.print(text)
    console.print()


@app.command("c")
def context(
    ctx: typer.Context,
    question: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = DEFAULT_CONTEXT_LIMIT,
    context: Annotated[
        int,
        typer.Option("--context", help="Adjacent chunks around each lexical match."),
    ] = 0,
) -> None:
    data = core_from_context(ctx).build_context(question, limit, context=context).data
    print_text_block(str(data["markdown"]))


@app.command("t")
def topic_memory(
    ctx: typer.Context,
    query: str,
    alias: Annotated[
        list[str] | None,
        typer.Option("--alias", help="Add an alias for this topic."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    core = core_from_context(ctx)
    if alias:
        topic = core.add_topic_alias(query, alias).data
        if json_output:
            print_json(topic)
            return
        for added_alias in topic["added_aliases"]:
            print_text_block(f"alias added: {added_alias} -> {topic['title']}")
    memory = core.topic(query, limit).data
    if json_output:
        print_json(memory)
        return
    memory["ui_language"] = ui_language_from_context(ctx)
    print_topic_memory(memory)


app.command("topic", hidden=True)(topic_memory)


@app.command("open")
def open_command(
    ctx: typer.Context,
    meeting_id: str,
    source: Annotated[bool, typer.Option("--source", help="Open the indexed source path.")] = False,
    print_path: Annotated[
        bool,
        typer.Option("--print-path", help="Print the selected path without opening it."),
    ] = False,
) -> None:
    """Open the original meeting folder."""
    repo = IndexRepository(ctx.obj["index_path"])
    meeting = repo.get_meeting(meeting_id)
    if not meeting:
        message = f"Meeting not found: {meeting_id}"
        raise typer.BadParameter(message)
    path = meeting.get("source_path") if source else meeting.get("folder_path")
    path = path or meeting.get("folder_path") or meeting.get("source_path")
    if not path:
        message = f"Meeting has no path: {meeting_id}"
        raise typer.BadParameter(message)
    if print_path:
        print_text_block(str(path))
        return
    open_path(Path(path))
