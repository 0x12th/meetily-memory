from pathlib import Path
from typing import Annotated

import typer

from meetily_memory.cli.common import (
    compact_date,
    console,
    core_from_context,
    make_typer,
    open_path,
    print_json,
    print_text_block,
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
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    results = core_from_context(ctx).search(query, limit).data["results"]
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
    data = core_from_context(ctx).build_context(question, limit).data
    print_text_block(str(data["markdown"]))


@app.command("topic")
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
    print_topic_memory(memory)


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
