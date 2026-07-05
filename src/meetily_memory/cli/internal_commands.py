import sys
from typing import Annotated

import typer

from meetily_memory.cli.common import (
    ENTITY_COMMANDS,
    ENTITY_LABELS,
    compact_date,
    console,
    core_from_context,
    make_typer,
    meeting_label,
    print_json,
    print_meeting_table,
    print_text_block,
)
from meetily_memory.cli.renderers import (
    entity_source,
    graph_node_title,
)

app = make_typer("Internal debug commands.")


def list_meetings(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    rows = core_from_context(ctx).meetings(limit).data["meetings"]
    if json_output:
        print_json(rows)
        return
    print_meeting_table(rows)


def last(
    ctx: typer.Context,
    person: Annotated[str | None, typer.Option("--person")] = None,
    transcript: Annotated[bool, typer.Option("--transcript")] = False,
    summary: Annotated[bool, typer.Option("--summary")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    core = core_from_context(ctx)
    meeting = core.latest_meeting(person=person).data["meeting"]
    if not meeting:
        raise typer.Exit(1)
    if json_output:
        print_json(meeting)
        return
    console.print(meeting_label(meeting))
    console.print(f"open: mm open {meeting['id']}")
    if summary and meeting.get("summary_text"):
        console.print(meeting["summary_text"])
    if transcript:
        chunks = core.meeting_chunks(int(meeting["id"])).data["chunks"]
        for chunk in chunks:
            if chunk["kind"] == "transcript":
                console.print(chunk["text"])


def person(
    ctx: typer.Context,
    name: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    rows = core_from_context(ctx).meetings(limit=limit, person=name).data["meetings"]
    if json_output:
        print_json(rows)
        return
    print_meeting_table(rows)


def local_summary(
    ctx: typer.Context,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    memory = core_from_context(ctx).summary().data
    if json_output:
        print_json(memory)
        return
    stats = memory["stats"]
    print_text_block("Local memory summary")
    print_text_block(f"meetings: {stats['meetings']}")
    print_text_block(f"chunks: {stats['chunks']}")
    if memory["latest_meeting"]:
        print_text_block(f"latest meeting: {meeting_label(memory['latest_meeting'])}")
    print_text_block(f"decisions: {stats['decisions']}")
    print_text_block(f"action items: {stats['action_items']}")
    print_text_block(f"risks: {stats['risks']}")
    print_text_block(f"open questions: {stats['open_questions']}")


def timeline(
    ctx: typer.Context,
    query: Annotated[str | None, typer.Argument(help="Optional project/topic filter.")] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    rows = core_from_context(ctx).timeline(query, limit).data["signals"]
    if json_output:
        print_json(rows)
        return
    if not rows:
        console.print("No timeline signals found. Run `mm analyze` after scanning.")
        return
    for row in rows:
        print_entity_timeline_row(row)


def project_memory(
    ctx: typer.Context,
    query: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    memory = core_from_context(ctx).project(query, limit).data
    if json_output:
        print_json(memory)
        return
    print_text_block(f"Project memory: {query}")
    print_text_block("\nMeetings")
    print_search_meeting_summaries(memory["meetings"])
    print_text_block("Structured signals")
    print_entity_bullets(memory["structured_signals"])


def person_memory(
    ctx: typer.Context,
    name: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    memory = core_from_context(ctx).person(name, limit).data
    if json_output:
        print_json(memory)
        return
    print_text_block(f"Person memory: {name}")
    print_text_block("\nLatest meetings")
    print_meeting_summaries(memory["meetings"])
    print_grouped_entity_bullets(memory["structured_signals"])


def graph(
    ctx: typer.Context,
    query: str,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 50,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    payload = core_from_context(ctx).graph(query, limit).data
    if json_output:
        print_json(payload)
        return
    print_text_block(f"Graph: {payload['topic']['title']}")
    for edge in payload["edges"]:
        from_node = graph_node_title(payload["nodes"], int(edge["from_node_id"]))
        to_node = graph_node_title(payload["nodes"], int(edge["to_node_id"]))
        print_text_block(f"- [[{from_node}]] --{edge['relation']}--> [[{to_node}]]")


def task_status(
    ctx: typer.Context,
    task_id: int,
    status: str,
    note: Annotated[str | None, typer.Option("--note")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        row = core_from_context(ctx).set_task_status(task_id, status, note=note).data
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        print_json(row)
        return
    print_text_block(f"task status: {row['status']}")
    print_text_block(str(row["text"]))
    if row.get("status_note"):
        print_text_block(str(row["status_note"]))


def decisions(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    print_structured_entities(ctx, ENTITY_COMMANDS["decisions"], limit, json_output=json_output)


def tasks(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    status: Annotated[str, typer.Option("--status")] = "open",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    print_structured_entities(
        ctx,
        ENTITY_COMMANDS["tasks"],
        limit,
        json_output=json_output,
        status=status,
    )


def risks(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 20,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    print_structured_entities(ctx, ENTITY_COMMANDS["risks"], limit, json_output=json_output)


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
    status: str = "all",
) -> None:
    try:
        rows = (
            core_from_context(ctx).structured_entities(kind, limit, status=status).data["entities"]
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
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
        if kind == "action_items":
            print_text_block(f"status: {row.get('status', 'open')}")
            if row.get("status_note"):
                print_text_block(f"status note: {row['status_note']}")
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
        print_text_block(f"- {label}: {row['text']} | Source: {entity_source(row)}")


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
