from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.json_codec import dumps_json
from meetily_memory.spotlight_export import slugify


@dataclass(frozen=True)
class FileExportResult:
    path: Path
    format: str

    def as_payload(self) -> dict[str, str]:
        return {
            "path": str(self.path),
            "format": self.format,
        }


@dataclass(frozen=True)
class MultiFileExportResult:
    output_dir: Path
    files: list[Path]
    format: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "files": [str(path) for path in self.files],
            "format": self.format,
        }


def export_obsidian_topic(
    index_path: Path,
    topic: str,
    output_dir: Path,
    *,
    limit: int = 10,
) -> MultiFileExportResult:
    core = MeetilyMemoryCore(index_path)
    topic_payload = core.topic(topic, limit).data
    graph_payload = core.graph(topic).data
    output_dir.mkdir(parents=True, exist_ok=True)
    topic_path = output_dir / f"{slugify(str(topic_payload['topic']['title'])) or 'topic'}.md"
    topic_path.write_text(
        render_obsidian_topic(topic_payload, graph_payload),
        encoding="utf-8",
    )
    return MultiFileExportResult(output_dir=output_dir, files=[topic_path], format="obsidian")


def export_gbrain_bundle(
    index_path: Path,
    query: str,
    output_path: Path,
    *,
    limit: int = 10,
) -> FileExportResult:
    core = MeetilyMemoryCore(index_path)
    records = [
        core.topic(query, limit).as_payload(),
        core.build_context(f"What do we know about {query}?", limit).as_payload(),
        core.graph(query).as_payload(),
        core.timeline(query, limit).as_payload(),
    ]
    write_text_file(output_path, "\n".join(dumps_json(record) for record in records) + "\n")
    return FileExportResult(path=output_path, format="gbrain-jsonl")


def export_markdown_bundle(
    index_path: Path,
    query: str,
    output_path: Path,
    *,
    limit: int = 10,
) -> FileExportResult:
    core = MeetilyMemoryCore(index_path)
    topic_payload = core.topic(query, limit).data
    context_payload = core.build_context(f"What do we know about {query}?", limit).data
    timeline_payload = core.timeline(query, limit).data
    write_text_file(
        output_path,
        render_markdown_bundle(query, topic_payload, context_payload, timeline_payload),
    )
    return FileExportResult(path=output_path, format="markdown")


def export_task_tracker_draft(
    index_path: Path,
    *,
    task_query: str,
    output_path: Path,
    tracker: str = "generic",
    limit: int = 20,
) -> FileExportResult:
    core = MeetilyMemoryCore(index_path)
    task_payload = core.structured_entities("action_items", limit, status="open").data
    matching_tasks = [
        task for task in task_payload["entities"] if row_matches_text(task, task_query)
    ]
    selected_task = matching_tasks[0] if matching_tasks else None
    context_payload = core.build_context(task_query, limit=5).data
    write_text_file(
        output_path,
        render_task_tracker_draft(tracker, task_query, selected_task, context_payload),
    )
    return FileExportResult(path=output_path, format="task-tracker-draft")


def render_obsidian_topic(
    topic_payload: dict[str, Any],
    graph_payload: dict[str, Any],
) -> str:
    topic = topic_payload["topic"]
    lines = [
        f"# {topic['title']}",
        "",
        "<!-- meetily-memory obsidian export -->",
        "",
        "## Summary",
        "",
        f"- Topic: [[{topic['title']}]]",
        "- Source: Meetily Memory Core API",
    ]
    related_meetings = unique_meeting_titles(topic_payload.get("meetings", []))
    if related_meetings:
        lines.extend(["", "## Related Meetings", ""])
        lines.extend(f"- [[{title}]]" for title in related_meetings)

    lines.extend(render_signal_sections(topic_payload.get("structured_signals", []), obsidian=True))

    edges = graph_payload.get("edges", [])
    if edges:
        lines.extend(["", "## Graph Edges", ""])
        nodes = [
            cast("dict[str, Any]", node)
            for node in graph_payload.get("nodes", [])
            if isinstance(node, dict)
        ]
        node_titles = {int(node["id"]): str(node["title"]) for node in nodes}
        for edge in edges:
            if not isinstance(edge, dict):
                continue
            from_title = node_titles.get(int(edge["from_node_id"]), str(edge["from_node_id"]))
            to_title = node_titles.get(int(edge["to_node_id"]), str(edge["to_node_id"]))
            lines.append(f"- [[{from_title}]] --{edge['relation']}--> [[{to_title}]]")

    return "\n".join(lines).rstrip() + "\n"


def render_markdown_bundle(
    query: str,
    topic_payload: dict[str, Any],
    context_payload: dict[str, Any],
    timeline_payload: dict[str, Any],
) -> str:
    lines = [
        f"# Meetily Memory: {query}",
        "",
        "<!-- meetily-memory markdown export -->",
        "",
        "## Topic Summary",
        "",
    ]
    lines.extend(
        render_signal_sections(topic_payload.get("structured_signals", []), obsidian=False)
    )
    lines.extend(["", "## Timeline", ""])
    signals = timeline_payload.get("signals", [])
    if signals:
        lines.extend(
            f"- {signal.get('meeting_date') or ''}: {signal['text']} ({source_label(signal)})"
            for signal in signals
        )
    else:
        lines.append("No timeline signals.")
    lines.extend(["", "## LLM Context", "", str(context_payload["markdown"])])
    return "\n".join(lines).rstrip() + "\n"


def render_task_tracker_draft(
    tracker: str,
    task_query: str,
    task: dict[str, Any] | None,
    context_payload: dict[str, Any],
) -> str:
    title = str(task["text"]) if task else task_query
    lines = [
        "# Task Draft",
        "",
        f"Tracker: {tracker}",
        f"Title: {title}",
        "",
        "## Description",
        "",
        title,
    ]
    if task:
        lines.extend(
            [
                "",
                "## Source Evidence",
                "",
                f"- Source: {source_label(task)}",
                f"- Status: {task.get('status', 'open')}",
                f"- Confidence: {float(task['confidence']):.2f}",
            ]
        )
    lines.extend(["", "## Context", "", str(context_payload["markdown"])])
    lines.extend(
        [
            "",
            "## Integration Notes",
            "",
            "- Draft only; no tracker write-back was performed.",
            "- Copy this into Jira, Yandex Tracker, or another task tracker.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def render_signal_sections(rows: object, *, obsidian: bool) -> list[str]:
    if not isinstance(rows, list):
        return []
    row_dicts = [cast("dict[str, Any]", row) for row in rows if isinstance(row, dict)]
    groups = {
        "decisions": "Latest Decisions",
        "action_items": "Unresolved Tasks",
        "risks": "Active Risks",
        "open_questions": "Open Questions",
    }
    lines: list[str] = []
    for kind, heading in groups.items():
        matching = [row for row in row_dicts if row.get("kind") == kind]
        if not matching:
            continue
        lines.extend(["", f"## {heading}", ""])
        for row in matching:
            text = str(row["text"])
            source = source_label(row)
            if obsidian and row.get("meeting_title"):
                lines.append(f"- {text} - [[{row['meeting_title']}]] - Source: {source}")
            else:
                lines.append(f"- {text} - Source: {source}")
    return lines


def unique_meeting_titles(rows: object) -> list[str]:
    if not isinstance(rows, list):
        return []
    row_dicts = [cast("dict[str, Any]", row) for row in rows if isinstance(row, dict)]
    titles: list[str] = []
    seen: set[str] = set()
    for row in row_dicts:
        title = row.get("title")
        if title is None:
            continue
        value = str(title)
        if value not in seen:
            seen.add(value)
            titles.append(value)
    return titles


def source_label(row: dict[str, Any]) -> str:
    source_parts = [
        str(row.get("meeting_external_id") or row.get("meeting_id") or ""),
        str(row.get("chunk_external_id") or row.get("source_chunk_id") or ""),
    ]
    source = " / ".join(part for part in source_parts if part)
    if row.get("chunk_timestamp_label"):
        return f"{source} @ {row['chunk_timestamp_label']}"
    return source


def row_matches_text(row: dict[str, Any], query: str) -> bool:
    terms = [term.casefold() for term in query.split() if term.strip()]
    haystack = " ".join(
        str(row.get(key) or "")
        for key in (
            "text",
            "meeting_title",
            "meeting_external_id",
            "chunk_external_id",
            "chunk_speaker",
        )
    ).casefold()
    return all(term in haystack for term in terms)


def write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
