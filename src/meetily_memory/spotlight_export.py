import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meetily_memory.db.repository import IndexRepository

EXPORT_FILE_GLOB = "meetily-memory-*.md"
ENTITY_HEADINGS = {
    "decisions": "Decisions",
    "action_items": "Action Items",
    "risks": "Risks",
    "open_questions": "Open Questions",
}
FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class SpotlightExportResult:
    output_dir: Path
    meetings_exported: int
    files_removed: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "meetings_exported": self.meetings_exported,
            "files_removed": self.files_removed,
        }


@dataclass(frozen=True)
class SpotlightCleanResult:
    output_dir: Path
    files_removed: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "output_dir": str(self.output_dir),
            "files_removed": self.files_removed,
        }


def default_spotlight_output_dir() -> Path:
    return Path.home() / "Documents" / "Meetily Memory"


def export_spotlight_markdown(
    repo: IndexRepository,
    output_dir: Path | None = None,
    *,
    include_transcript: bool = True,
) -> SpotlightExportResult:
    target_dir = output_dir or default_spotlight_output_dir()
    removed = clean_spotlight_markdown(target_dir).files_removed
    target_dir.mkdir(parents=True, exist_ok=True)

    exported = 0
    for meeting_id in repo.list_meeting_ids():
        meeting = repo.get_meeting(str(meeting_id))
        if meeting is None:
            continue
        chunks = repo.get_chunks_for_meeting(meeting_id)
        entities = repo.list_structured_entities(meeting_id)
        file_path = target_dir / meeting_filename(meeting)
        file_path.write_text(
            render_meeting_markdown(
                meeting,
                chunks,
                entities,
                include_transcript=include_transcript,
            ),
            encoding="utf-8",
        )
        exported += 1

    return SpotlightExportResult(target_dir, exported, removed)


def clean_spotlight_markdown(output_dir: Path | None = None) -> SpotlightCleanResult:
    target_dir = output_dir or default_spotlight_output_dir()
    removed = 0
    if target_dir.exists():
        for path in target_dir.glob(EXPORT_FILE_GLOB):
            if path.is_file():
                path.unlink()
                removed += 1
    return SpotlightCleanResult(target_dir, removed)


def render_meeting_markdown(
    meeting: dict[str, Any],
    chunks: list[dict[str, Any]],
    entities: list[dict[str, Any]],
    *,
    include_transcript: bool,
) -> str:
    lines = [
        f"# {meeting['title']}",
        "",
        "<!-- meetily-memory spotlight export -->",
        "",
        f"Date: {meeting_date(meeting)}",
        f"Meeting ID: {meeting['id']}",
        f"External ID: {meeting['external_id']}",
        f"Open: mm open {meeting['id']}",
    ]
    if meeting.get("folder_path"):
        lines.append(f"Folder: {meeting['folder_path']}")
    if meeting.get("source_path"):
        lines.append(f"Source DB: {meeting['source_path']}")

    if meeting.get("summary_text"):
        lines.extend(["", "## Summary", "", str(meeting["summary_text"])])

    lines.extend(render_entity_sections(entities))

    if include_transcript:
        transcript_lines = render_transcript(chunks)
        if transcript_lines:
            lines.extend(["", "## Transcript", "", *transcript_lines])

    return "\n".join(lines).rstrip() + "\n"


def render_entity_sections(entities: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for kind, heading in ENTITY_HEADINGS.items():
        matching = [entity for entity in entities if entity["kind"] == kind]
        if not matching:
            continue
        lines.extend(["", f"## {heading}", ""])
        lines.extend(f"- {entity['text']}" for entity in matching)
    return lines


def render_transcript(chunks: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for chunk in chunks:
        if chunk["kind"] != "transcript":
            continue
        prefix_parts = []
        if chunk.get("timestamp_label"):
            prefix_parts.append(str(chunk["timestamp_label"]))
        if chunk.get("speaker"):
            prefix_parts.append(str(chunk["speaker"]))
        prefix = f"{' / '.join(prefix_parts)}: " if prefix_parts else ""
        lines.append(f"- {prefix}{chunk['text']}")
    return lines


def meeting_filename(meeting: dict[str, Any]) -> str:
    date = compact_date(meeting_date(meeting))
    title = slugify(str(meeting["title"])) or "meeting"
    return f"meetily-memory-{int(meeting['id']):06d}-{date}-{title}.md"


def meeting_date(meeting: dict[str, Any]) -> str:
    return str(
        meeting.get("updated_at") or meeting.get("created_at") or meeting.get("indexed_at") or ""
    )


def compact_date(value: str) -> str:
    return value[:10] if value else "unknown-date"


def slugify(value: str) -> str:
    normalized = FILENAME_SAFE_RE.sub("-", value.strip())
    return normalized.strip("-._").lower()[:80]
