from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.db.repository import IndexRepository


@dataclass(frozen=True)
class ObsidianSyncResult:
    root_dir: Path
    files_written: int
    files_skipped: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "root_dir": str(self.root_dir),
            "files_written": self.files_written,
            "files_skipped": self.files_skipped,
        }


MANAGED_MARKER = "<!-- meetily-memory:managed -->"
OBSIDIAN_DIRS = (
    "Topics",
    "Meetings",
    "People",
    "Tasks",
    "Decisions",
    "Risks",
    "Questions",
)


def sync_obsidian_vault(
    index_path: Path,
    vault_path: Path,
    folder: str = "Meetily Memory",
    *,
    limit: int = 100,
) -> ObsidianSyncResult:
    root_dir = vault_path.expanduser() / folder
    for directory in OBSIDIAN_DIRS:
        (root_dir / directory).mkdir(parents=True, exist_ok=True)

    core = MeetilyMemoryCore(index_path)
    repo = core.repo
    files_written = 0
    files_skipped = 0
    people: dict[str, dict[str, Any]] = {}

    written, skipped = sync_obsidian_meetings(root_dir, repo, limit)
    files_written += written
    files_skipped += skipped

    written, skipped, topic_people = sync_obsidian_topics(root_dir, core, limit)
    files_written += written
    files_skipped += skipped
    people.update(topic_people)

    written, skipped, entity_people = sync_obsidian_entities(root_dir, repo, limit)
    files_written += written
    files_skipped += skipped
    people.update(entity_people)

    written, skipped = sync_obsidian_people(root_dir, people)
    files_written += written
    files_skipped += skipped

    return ObsidianSyncResult(
        root_dir=root_dir,
        files_written=files_written,
        files_skipped=files_skipped,
    )


def sync_obsidian_meetings(
    root_dir: Path,
    repo: IndexRepository,
    limit: int,
) -> tuple[int, int]:
    files_written = 0
    files_skipped = 0
    for meeting in repo.list_meetings(limit=limit):
        path = root_dir / "Meetings" / f"{safe_note_name(str(meeting['title']))}.md"
        written = write_managed_note(path, render_obsidian_meeting_note(meeting))
        files_written += int(written)
        files_skipped += int(not written)
    return files_written, files_skipped


def sync_obsidian_topics(
    root_dir: Path,
    core: MeetilyMemoryCore,
    limit: int,
) -> tuple[int, int, dict[str, dict[str, Any]]]:
    files_written = 0
    files_skipped = 0
    people: dict[str, dict[str, Any]] = {}
    for topic in core.repo.list_topics(limit=limit):
        title = str(topic["title"])
        payload = core.topic(title).data
        path = root_dir / "Topics" / f"{safe_note_name(title)}.md"
        written = write_managed_note(path, render_obsidian_topic_memory(payload))
        files_written += int(written)
        files_skipped += int(not written)
        people.update(people_from_topic_payload(payload))
    return files_written, files_skipped, people


def sync_obsidian_entities(
    root_dir: Path,
    repo: IndexRepository,
    limit: int,
) -> tuple[int, int, dict[str, dict[str, Any]]]:
    files_written = 0
    files_skipped = 0
    people: dict[str, dict[str, Any]] = {}
    entity_dirs = {
        "action_items": "Tasks",
        "decisions": "Decisions",
        "risks": "Risks",
        "open_questions": "Questions",
    }
    for entity in repo.list_all_structured_entity_details(limit=limit):
        directory = entity_dirs.get(str(entity["kind"]))
        if directory is None:
            continue
        filename = safe_note_name(str(entity["text"])[:80]) or f"{entity['kind']}-{entity['id']}"
        path = root_dir / directory / f"{filename}.md"
        written = write_managed_note(path, render_obsidian_entity_note(entity))
        files_written += int(written)
        files_skipped += int(not written)
        for person in payload_people(entity):
            people[person.casefold()] = {"display_name": person}
    return files_written, files_skipped, people


def sync_obsidian_people(
    root_dir: Path,
    people: dict[str, dict[str, Any]],
) -> tuple[int, int]:
    files_written = 0
    files_skipped = 0
    for person in people.values():
        display_name = str(person["display_name"])
        path = root_dir / "People" / f"{safe_note_name(display_name)}.md"
        written = write_managed_note(path, render_obsidian_person_note(display_name))
        files_written += int(written)
        files_skipped += int(not written)
    return files_written, files_skipped


def write_managed_note(path: Path, text: str) -> bool:
    if path.exists() and MANAGED_MARKER not in path.read_text(encoding="utf-8"):
        return False
    write_text_file(path, text)
    return True


def render_obsidian_meeting_note(meeting: dict[str, Any]) -> str:
    lines = [
        f"# {meeting['title']}",
        "",
        MANAGED_MARKER,
        "",
        f"- Meetily ID: `{meeting['external_id']}`",
        f"- Date: {meeting.get('updated_at') or meeting.get('created_at') or ''}",
        f"- Open: `mm open {meeting['id']}`",
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_obsidian_topic_memory(topic_payload: dict[str, Any]) -> str:
    topic = topic_payload["topic"]
    lines = [
        f"# {topic['title']}",
        "",
        MANAGED_MARKER,
        "",
        "## Meetings",
        "",
    ]
    meetings = unique_meeting_titles(topic_payload.get("meetings", []))
    lines.extend(f"- [[{title}]]" for title in meetings)
    lines.extend(render_signal_sections(topic_payload.get("structured_signals", []), obsidian=True))
    people = topic_payload.get("related_people", [])
    if isinstance(people, list) and people:
        lines.extend(["", "## People", ""])
        lines.extend(
            f"- [[{person['display_name']}]]"
            for person in people
            if isinstance(person, dict) and person.get("display_name")
        )
    return "\n".join(lines).rstrip() + "\n"


def render_obsidian_entity_note(entity: dict[str, Any]) -> str:
    title = str(entity["text"])
    lines = [
        f"# {title}",
        "",
        MANAGED_MARKER,
        "",
        f"- Kind: `{entity['kind']}`",
        f"- Meeting: [[{entity['meeting_title']}]]",
        f"- Source: {source_label(entity)}",
        "",
        title,
    ]
    if entity.get("status"):
        lines.insert(6, f"- Status: `{entity['status']}`")
    return "\n".join(lines).rstrip() + "\n"


def render_obsidian_person_note(display_name: str) -> str:
    lines = [
        f"# {display_name}",
        "",
        MANAGED_MARKER,
        "",
        f"- Person: {display_name}",
    ]
    return "\n".join(lines).rstrip() + "\n"


def payload_people(entity: dict[str, Any]) -> list[str]:
    value = entity.get("assignee") or entity.get("owner") or entity.get("person")
    return [str(value)] if value else []


def people_from_topic_payload(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    people: dict[str, dict[str, Any]] = {}
    related_people = payload.get("related_people", [])
    if not isinstance(related_people, list):
        return people
    for person in related_people:
        if isinstance(person, dict) and person.get("display_name"):
            display_name = str(person["display_name"])
            people[display_name.casefold()] = {"display_name": display_name}
    return people


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


def write_text_file(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def safe_note_name(value: str) -> str:
    return "".join("_" if char in '/\\:*?"<>|' else char for char in value).strip()
