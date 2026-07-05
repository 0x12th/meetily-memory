from typing import cast

from meetily_memory.cli.common import compact_date, print_text_block


def print_topic_memory(memory: dict[str, object]) -> None:
    topic = cast("dict[str, object]", memory["topic"])
    print_text_block(f"Topic memory: {topic['title']}")
    aliases = cast("list[str]", topic.get("aliases", []))
    for alias in aliases:
        print_text_block(f"alias: {alias}")
    print_text_block("\nRelated meetings")
    print_search_meeting_summaries(cast("list[dict[str, object]]", memory["meetings"]))
    print_text_block("Latest decisions")
    print_entity_bullets(entity_rows_for_kind(memory, "decisions"))
    print_text_block("Unresolved tasks")
    open_tasks = [
        row
        for row in entity_rows_for_kind(memory, "action_items")
        if row.get("status", "open") in {"open", "unknown"}
    ]
    print_entity_bullets(open_tasks)
    print_text_block("Active risks")
    print_entity_bullets(entity_rows_for_kind(memory, "risks"))
    print_text_block("Open questions")
    print_entity_bullets(entity_rows_for_kind(memory, "open_questions"))
    people = cast("list[dict[str, object]]", memory.get("related_people", []))
    if people:
        print_text_block("Related people")
        for person in people:
            print_text_block(f"- {person['display_name']}")


def entity_rows_for_kind(memory: dict[str, object], kind: str) -> list[dict[str, object]]:
    rows = cast("list[dict[str, object]]", memory.get("structured_signals", []))
    return [row for row in rows if row["kind"] == kind]


def graph_node_title(nodes: list[dict[str, object]], node_id: int) -> str:
    for node in nodes:
        if int(str(node["id"])) == node_id:
            return str(node["title"])
    return str(node_id)


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


def print_search_meeting_summaries(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No matching meetings.")
        return
    seen: set[int] = set()
    for row in rows:
        meeting_id = int(cast("int | str", row["meeting_id"]))
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
        print_text_block(f"- #{row['id']} {row['title']} | open: mm open {row['id']}")


def print_entity_bullets(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No structured signals.")
        return
    for row in rows:
        print_text_block(f"- {row['text']} | Source: {entity_source(row)}")


def print_grouped_entity_bullets(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No structured signals.")
        return
    for row in rows:
        kind = str(row.get("kind", "signal")).replace("_", " ")
        print_text_block(f"- {kind}: {row['text']} | Source: {entity_source(row)}")
