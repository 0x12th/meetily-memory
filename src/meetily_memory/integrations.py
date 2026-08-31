import base64
import binascii
import hashlib
import re
import unicodedata
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from meetily_memory.domain import MeetingRef
from meetily_memory.json_codec import dumps_json, loads_json
from meetily_memory.open_commands import markdown_inline_code, stable_meeting_open_command
from meetily_memory.repositories.snapshot import SnapshotRepository

OBSIDIAN_MARKER_VERSION = 2
OBJECT_KEY_VERSION = 2
LEGACY_OBSIDIAN_MARKER_VERSION = 1
LEGACY_OBJECT_KEY_VERSION = 1
OBSIDIAN_DIRS = ("Meetings", "Tags")
LEGACY_OBSIDIAN_DIRS = (
    "Topics",
    "People",
    "Tasks",
    "Decisions",
    "Risks",
    "Questions",
)
OBSIDIAN_SCAN_DIRS = OBSIDIAN_DIRS + LEGACY_OBSIDIAN_DIRS
MANAGED_MARKER = f"<!-- meetily-memory:managed:v{OBSIDIAN_MARKER_VERSION}:"
MANAGED_MARKER_RE = re.compile(
    rf"^<!-- meetily-memory:managed:v{OBSIDIAN_MARKER_VERSION}:([A-Za-z0-9_-]+) -->$"
)
LEGACY_MANAGED_MARKER_RE = re.compile(
    rf"^<!-- meetily-memory:managed:v{LEGACY_OBSIDIAN_MARKER_VERSION}:"
    r"([A-Za-z0-9_-]+) -->$"
)
MAX_FILENAME_COMPONENT_BYTES = 255
NOTE_EXTENSION = ".md"
SUFFIX_DIGEST_HEX_LENGTH = 20
ASCII_CONTROL_LIMIT = 32
ASCII_DELETE = 127
TEMP_PATH_ATTEMPTS = 1000
FORBIDDEN_FILENAME_CHARS = frozenset('<>:"/\\|?*#^[]')
FORBIDDEN_WIKILINK_ALIAS_CHARS = frozenset("\\|[]#^")
WINDOWS_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{number}" for number in range(1, 10)}
    | {f"lpt{number}" for number in range(1, 10)}
)
ENTITY_DIRS = {
    "action_items": "Tasks",
    "decisions": "Decisions",
    "risks": "Risks",
    "open_questions": "Questions",
}
IDENTITY_SCHEMAS = {
    "meeting": frozenset({"version", "kind", "source_uuid", "external_id"}),
    "tag": frozenset({"version", "kind", "normalized_name"}),
}
LEGACY_IDENTITY_SCHEMAS = {
    "meeting": frozenset({"version", "kind", "source_uuid", "external_id"}),
    "entity": frozenset(
        {
            "version",
            "kind",
            "source_uuid",
            "meeting_external_id",
            "chunk_evidence_id",
            "entity_kind",
            "stable_content_fingerprint",
        }
    ),
    "topic": frozenset({"version", "kind", "stable_key"}),
    "person": frozenset({"version", "kind", "stable_key"}),
}


@dataclass(frozen=True)
class ObsidianTag:
    normalized_name: str
    display_name: str


@dataclass(frozen=True)
class ObsidianMeetingSnapshot:
    ref: MeetingRef
    title: str
    started_at: str | None
    ended_at: str | None
    created_at: str | None
    updated_at: str | None
    source_summary: str | None
    manual_tags: tuple[ObsidianTag, ...]


@dataclass(frozen=True)
class ObsidianTagSnapshot:
    tag: ObsidianTag
    meetings: tuple[ObsidianMeetingSnapshot, ...]


@dataclass(frozen=True)
class ObsidianSnapshot:
    meetings: tuple[ObsidianMeetingSnapshot, ...]
    tags: tuple[ObsidianTagSnapshot, ...]


@dataclass(frozen=True)
class ObsidianSyncResult:
    root_dir: Path
    files_written: int
    files_skipped: int
    files_removed: int

    def as_payload(self) -> dict[str, Any]:
        return {
            "root_dir": str(self.root_dir),
            "files_written": self.files_written,
            "files_skipped": self.files_skipped,
            "files_removed": self.files_removed,
        }


@dataclass(frozen=True)
class NoteRef:
    object_key: str
    directory: str
    display_label: str
    suffix_kind: str
    stem: str = field(init=False)
    relative_path: Path = field(init=False)
    wikilink: str = field(init=False)
    identity_marker: str = field(init=False)

    def __post_init__(self) -> None:
        if self.directory not in OBSIDIAN_DIRS:
            message = f"Unknown Obsidian note directory: {self.directory}"
            raise ValueError(message)
        if not is_canonical_object_key(self.object_key):
            message = "Obsidian note object key must use the canonical v2 meeting/tag schema."
            raise ValueError(message)
        label = normalize_display_label(self.display_label)
        digest = hashlib.sha256(self.object_key.encode()).hexdigest()
        suffix = f"--{self.suffix_kind}-{digest[:SUFFIX_DIGEST_HEX_LENGTH]}"
        stem = filename_stem(label, suffix)
        encoded_key = base64.urlsafe_b64encode(self.object_key.encode()).decode().rstrip("=")
        object.__setattr__(self, "display_label", label)
        object.__setattr__(self, "stem", stem)
        object.__setattr__(self, "relative_path", Path(self.directory) / f"{stem}{NOTE_EXTENSION}")
        object.__setattr__(self, "wikilink", f"[[{stem}|{wikilink_alias(label)}]]")
        object.__setattr__(self, "identity_marker", f"{MANAGED_MARKER}{encoded_key} -->")

    @classmethod
    def meeting(cls, source_uuid: str, external_id: str, title: str) -> "NoteRef":
        return cls(
            object_key=note_object_key(
                "meeting",
                source_uuid=source_uuid,
                external_id=external_id,
            ),
            directory="Meetings",
            display_label=title,
            suffix_kind="m",
        )

    @classmethod
    def tag(cls, normalized_name: str, display_name: str) -> "NoteRef":
        return cls(
            object_key=note_object_key("tag", normalized_name=normalized_name),
            directory="Tags",
            display_label=display_name,
            suffix_kind="g",
        )


@dataclass(frozen=True)
class PlannedNote:
    ref: NoteRef
    text: str


@dataclass(frozen=True)
class _MarkerIdentity:
    marker_version: int
    object_key: str
    payload: dict[object, object]


@dataclass(frozen=True)
class _PlannedRemoval:
    path: Path
    replacement: Path | None
    temporary: Path | None


@dataclass(frozen=True)
class _VaultPreflight:
    writable: tuple[PlannedNote, ...]
    removals: tuple[_PlannedRemoval, ...]
    files_skipped: int


def note_object_key(kind: str, **identity: str) -> str:
    payload: dict[str, object] = {
        "version": OBJECT_KEY_VERSION,
        "kind": kind,
        **identity,
    }
    if not is_valid_identity_payload(payload):
        message = f"Invalid Obsidian {kind} note identity."
        raise ValueError(message)
    return dumps_json(payload)


def normalize_display_label(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    label = " ".join(normalized.split())
    return label or "Untitled"


def filename_stem(label: str, suffix: str) -> str:
    readable = "".join(
        "_"
        if char in FORBIDDEN_FILENAME_CHARS
        or ord(char) < ASCII_CONTROL_LIMIT
        or ord(char) == ASCII_DELETE
        else char
        for char in label
    ).strip(" .")
    if not readable or readable in {".", ".."}:
        readable = "Untitled"
    if readable.casefold().split(".", maxsplit=1)[0] in WINDOWS_RESERVED_NAMES:
        readable = f"_{readable}"
    readable_budget = (
        MAX_FILENAME_COMPONENT_BYTES - len(suffix.encode()) - len(NOTE_EXTENSION.encode())
    )
    readable = truncate_utf8(readable, readable_budget).rstrip(" .") or "Untitled"
    stem = f"{readable}{suffix}"
    if len(f"{stem}{NOTE_EXTENSION}".encode()) > MAX_FILENAME_COMPONENT_BYTES:
        message = "Obsidian note filename exceeds the UTF-8 component limit."
        raise ValueError(message)
    return stem


def truncate_utf8(value: str, byte_limit: int) -> str:
    result: list[str] = []
    used = 0
    for char in value:
        char_bytes = len(char.encode())
        if used + char_bytes > byte_limit:
            break
        result.append(char)
        used += char_bytes
    return "".join(result)


def wikilink_alias(label: str) -> str:
    return "".join("_" if char in FORBIDDEN_WIKILINK_ALIAS_CHARS else char for char in label)


def sync_obsidian_vault(
    index_path: Path,
    vault_path: Path,
    folder: str = "Meetily Memory",
    *,
    limit: int | None = None,
) -> ObsidianSyncResult:
    root_dir = obsidian_root_dir(vault_path, folder)
    effective_limit = limit if limit is not None else 2**31 - 1
    snapshot = build_obsidian_snapshot(index_path, effective_limit)
    plan = build_obsidian_note_plan(snapshot)
    return apply_obsidian_note_plan(root_dir, plan, destructive=limit is None)


def obsidian_root_dir(vault_path: Path, folder: str) -> Path:
    vault_root = vault_path.expanduser().resolve()
    folder_path = Path(folder)
    if folder_path.is_absolute():
        message = "Obsidian folder must be relative to the configured vault."
        raise ValueError(message)
    root_dir = (vault_root / folder_path).resolve()
    if not root_dir.is_relative_to(vault_root):
        message = "Obsidian folder must stay inside the configured vault."
        raise ValueError(message)
    return root_dir


def build_obsidian_snapshot(index_path: Path, limit: int) -> ObsidianSnapshot:
    repository_snapshot = SnapshotRepository(index_path).read(limit)
    meetings = tuple(
        ObsidianMeetingSnapshot(
            ref=meeting.ref,
            title=meeting.title,
            started_at=meeting.started_at,
            ended_at=meeting.ended_at,
            created_at=meeting.created_at,
            updated_at=meeting.updated_at,
            source_summary=meeting.source_summary,
            manual_tags=tuple(
                ObsidianTag(tag.normalized_name, tag.display_name) for tag in meeting.manual_tags
            ),
        )
        for meeting in repository_snapshot.meetings
    )
    meetings_by_tag: dict[ObsidianTag, list[ObsidianMeetingSnapshot]] = {}
    for meeting in meetings:
        for tag in meeting.manual_tags:
            meetings_by_tag.setdefault(tag, []).append(meeting)
    tags = tuple(
        ObsidianTagSnapshot(
            tag=tag,
            meetings=tuple(sorted(tag_meetings, key=meeting_snapshot_sort_key)),
        )
        for tag, tag_meetings in sorted(
            meetings_by_tag.items(),
            key=lambda item: (item[0].normalized_name, item[0].display_name),
        )
    )
    return ObsidianSnapshot(meetings=meetings, tags=tags)


def meeting_snapshot_sort_key(meeting: ObsidianMeetingSnapshot) -> tuple[str, str, str, str]:
    return (
        normalize_display_label(meeting.title).casefold(),
        normalize_display_label(meeting.title),
        meeting.ref.source_uuid,
        meeting.ref.external_id,
    )


def build_obsidian_note_plan(snapshot: ObsidianSnapshot) -> tuple[PlannedNote, ...]:
    notes: list[PlannedNote] = []
    for meeting in snapshot.meetings:
        ref = NoteRef.meeting(meeting.ref.source_uuid, meeting.ref.external_id, meeting.title)
        notes.append(PlannedNote(ref, render_obsidian_meeting_note(meeting, ref)))
    for tag_snapshot in snapshot.tags:
        ref = NoteRef.tag(
            tag_snapshot.tag.normalized_name,
            tag_snapshot.tag.display_name,
        )
        notes.append(PlannedNote(ref, render_obsidian_tag_note(tag_snapshot, ref)))
    ordered = tuple(sorted(notes, key=note_plan_sort_key))
    validate_note_plan(ordered)
    return ordered


def render_obsidian_meeting_note(
    meeting: ObsidianMeetingSnapshot,
    ref: NoteRef | None = None,
) -> str:
    note_ref = ref or NoteRef.meeting(
        meeting.ref.source_uuid,
        meeting.ref.external_id,
        meeting.title,
    )
    lines = [
        f"# {note_ref.display_label}",
        "",
        note_ref.identity_marker,
        "",
        "- MeetingRef: "
        + markdown_inline_code(f"{meeting.ref.source_uuid}/{meeting.ref.external_id}"),
        f"- Title: {note_ref.display_label}",
        f"- Created at: {meeting.created_at or ''}",
        f"- Updated at: {meeting.updated_at or ''}",
    ]
    if meeting.started_at is not None:
        lines.append(f"- Started at: {meeting.started_at}")
    if meeting.ended_at is not None:
        lines.append(f"- Ended at: {meeting.ended_at}")
    lines.append("- Open: " + markdown_inline_code(stable_meeting_open_command(meeting.ref)))
    if meeting.manual_tags:
        lines.extend(["", "## Tags", ""])
        for tag in meeting.manual_tags:
            tag_ref = NoteRef.tag(tag.normalized_name, tag.display_name)
            lines.append(f"- {tag_ref.wikilink}")
    if meeting.source_summary is not None:
        lines.extend(["", "## Source summary", ""])
        lines.extend(blockquote_lines(meeting.source_summary))
    return "\n".join(lines).rstrip() + "\n"


def render_obsidian_tag_note(tag_snapshot: ObsidianTagSnapshot, ref: NoteRef | None = None) -> str:
    note_ref = ref or NoteRef.tag(
        tag_snapshot.tag.normalized_name,
        tag_snapshot.tag.display_name,
    )
    lines = [
        f"# {note_ref.display_label}",
        "",
        note_ref.identity_marker,
        "",
        f"- Tag: {markdown_inline_code(tag_snapshot.tag.display_name)}",
        f"- Normalized tag: {markdown_inline_code(tag_snapshot.tag.normalized_name)}",
        "",
        "## Meetings",
        "",
    ]
    for meeting in tag_snapshot.meetings:
        meeting_ref = NoteRef.meeting(
            meeting.ref.source_uuid,
            meeting.ref.external_id,
            meeting.title,
        )
        lines.append(f"- {meeting_ref.wikilink}")
    return "\n".join(lines).rstrip() + "\n"


def blockquote_lines(value: str) -> list[str]:
    return [f"> {line}" if line else ">" for line in value.splitlines() or [""]]


def note_plan_sort_key(note: PlannedNote) -> tuple[str, str]:
    return (note.ref.relative_path.as_posix().casefold(), note.ref.object_key)


def validate_note_plan(plan: tuple[PlannedNote, ...]) -> None:
    object_keys: set[str] = set()
    path_keys: set[str] = set()
    for note in plan:
        if note.ref.object_key in object_keys:
            message = f"Duplicate Obsidian note object identity: {note.ref.object_key}"
            raise ValueError(message)
        path_key = portable_path_key(note.ref.relative_path)
        if path_key in path_keys:
            message = f"Duplicate Obsidian note path: {note.ref.relative_path}"
            raise ValueError(message)
        if note.ref.identity_marker not in note.text.splitlines():
            message = f"Obsidian note is missing its identity marker: {note.ref.relative_path}"
            raise ValueError(message)
        object_keys.add(note.ref.object_key)
        path_keys.add(path_key)


def apply_obsidian_note_plan(
    root_dir: Path,
    plan: tuple[PlannedNote, ...],
    *,
    destructive: bool,
) -> ObsidianSyncResult:
    validate_note_plan(plan)
    preflight = preflight_obsidian_vault(root_dir, plan, destructive=destructive)

    for directory in OBSIDIAN_DIRS:
        (root_dir / directory).mkdir(parents=True, exist_ok=True)
    for note in preflight.writable:
        (root_dir / note.ref.relative_path).write_text(note.text, encoding="utf-8")
    files_removed = remove_planned_notes(preflight.removals)

    return ObsidianSyncResult(
        root_dir=root_dir,
        files_written=len(preflight.writable),
        files_skipped=preflight.files_skipped,
        files_removed=files_removed,
    )


def preflight_obsidian_vault(
    root_dir: Path,
    plan: tuple[PlannedNote, ...],
    *,
    destructive: bool,
) -> _VaultPreflight:
    validate_obsidian_scan_directories(root_dir)
    existing_by_identity = existing_managed_notes(root_dir)
    expected_paths = {note.ref.object_key: root_dir / note.ref.relative_path for note in plan}
    writable, skipped_keys = writable_notes(plan, expected_paths)
    occupied_path_keys = existing_portable_path_keys(root_dir) | {
        portable_path_key(path) for path in expected_paths.values()
    }
    removals = (
        stale_managed_notes(
            existing_by_identity,
            expected_paths,
            skipped_keys,
            occupied_path_keys,
        )
        if destructive
        else set()
    )
    return _VaultPreflight(
        writable=tuple(writable),
        removals=tuple(sorted(removals, key=lambda removal: str(removal.path))),
        files_skipped=len(skipped_keys),
    )


def existing_managed_notes(root_dir: Path) -> dict[str, list[Path]]:
    notes: dict[str, list[Path]] = {}
    for directory in OBSIDIAN_SCAN_DIRS:
        directory_path = root_dir / directory
        if not directory_path.exists():
            continue
        for path in sorted(directory_path.glob(f"*{NOTE_EXTENSION}")):
            if path.is_symlink() or not path.is_file():
                message = f"Obsidian managed note candidate must be a regular file: {path}"
                raise ValueError(message)
            identity = owned_marker_identity(path, directory)
            if identity is None:
                continue
            object_key = migrated_object_key(identity)
            notes.setdefault(object_key, []).append(path)
    return notes


def owned_marker_identity(path: Path, directory: str) -> _MarkerIdentity | None:
    identity = marker_identity_from_note_text(path.read_text(encoding="utf-8"))
    if identity is None:
        return None
    kind = cast("str", identity.payload["kind"])
    if identity.marker_version == OBSIDIAN_MARKER_VERSION:
        expected_directory = "Meetings" if kind == "meeting" else "Tags"
    elif kind == "meeting":
        expected_directory = "Meetings"
    elif kind == "topic":
        expected_directory = "Topics"
    elif kind == "person":
        expected_directory = "People"
    else:
        expected_directory = ENTITY_DIRS[cast("str", identity.payload["entity_kind"])]
    return identity if directory == expected_directory else None


def migrated_object_key(identity: _MarkerIdentity) -> str:
    if (
        identity.marker_version == LEGACY_OBSIDIAN_MARKER_VERSION
        and identity.payload["kind"] == "meeting"
    ):
        return note_object_key(
            "meeting",
            source_uuid=cast("str", identity.payload["source_uuid"]),
            external_id=cast("str", identity.payload["external_id"]),
        )
    return identity.object_key


def writable_notes(
    plan: tuple[PlannedNote, ...],
    expected_paths: dict[str, Path],
) -> tuple[list[PlannedNote], set[str]]:
    writable: list[PlannedNote] = []
    skipped_keys: set[str] = set()
    for note in plan:
        path = expected_paths[note.ref.object_key]
        if not path.exists():
            writable.append(note)
            continue
        if path.is_symlink() or not path.is_file():
            message = f"Obsidian note destination is not a regular file: {path}"
            raise ValueError(message)
        existing_text = path.read_text(encoding="utf-8")
        existing_key = object_key_from_note_text(existing_text)
        if existing_key is None:
            skipped_keys.add(note.ref.object_key)
        elif existing_key != note.ref.object_key:
            message = f"Obsidian note path belongs to another managed object: {path}"
            raise ValueError(message)
        elif existing_text != note.text:
            writable.append(note)
    return writable, skipped_keys


def stale_managed_notes(
    existing_by_identity: dict[str, list[Path]],
    expected_paths: dict[str, Path],
    skipped_keys: set[str],
    occupied_path_keys: set[str],
) -> set[_PlannedRemoval]:
    removals: set[_PlannedRemoval] = set()
    for object_key, paths in existing_by_identity.items():
        if object_key not in expected_paths:
            removals.update(_PlannedRemoval(path, None, None) for path in paths)
            continue
        if object_key in skipped_keys:
            continue
        expected_path = expected_paths[object_key]
        for path in paths:
            if path == expected_path:
                continue
            temporary = None
            if paths_are_same_case_variant(path, expected_path):
                temporary = unique_case_rename_temp_path(
                    path.parent,
                    object_key,
                    occupied_path_keys,
                )
            removals.add(_PlannedRemoval(path, expected_path, temporary))
    return removals


def existing_portable_path_keys(root_dir: Path) -> set[str]:
    return {
        portable_path_key(path)
        for directory in OBSIDIAN_SCAN_DIRS
        if (directory_path := root_dir / directory).exists()
        for path in directory_path.iterdir()
    }


def unique_case_rename_temp_path(
    directory: Path,
    object_key: str,
    occupied_path_keys: set[str],
) -> Path:
    digest = hashlib.sha256(object_key.encode()).hexdigest()[:SUFFIX_DIGEST_HEX_LENGTH]
    for attempt in range(TEMP_PATH_ATTEMPTS):
        candidate = directory / f".meetily-memory-rename-{digest}-{attempt}{NOTE_EXTENSION}"
        candidate_key = portable_path_key(candidate)
        if candidate_key in occupied_path_keys or candidate.is_symlink() or candidate.exists():
            continue
        occupied_path_keys.add(candidate_key)
        return candidate
    message = f"Could not reserve a safe Obsidian rename path in {directory}."
    raise ValueError(message)


def remove_planned_notes(removals: tuple[_PlannedRemoval, ...]) -> int:
    files_removed = 0
    for removal in removals:
        if removal.temporary is not None:
            if removal.replacement is None or not paths_are_same_case_variant(
                removal.path,
                removal.replacement,
            ):
                message = f"Obsidian exact-name rename is no longer safe: {removal.path}"
                raise RuntimeError(message)
            rename_case_variant_exactly(
                removal.path,
                removal.temporary,
                removal.replacement,
            )
            continue
        removal.path.unlink()
        files_removed += 1
    return files_removed


def paths_are_same_case_variant(path: Path, replacement: Path) -> bool:
    if (
        path.parent != replacement.parent
        or path.name == replacement.name
        or portable_path_key(path) != portable_path_key(replacement)
        or path_entry_exists_exact(replacement)
    ):
        return False
    try:
        return path.samefile(replacement)
    except OSError:
        return False


def path_entry_exists_exact(path: Path) -> bool:
    if not path.parent.is_dir():
        return False
    return any(entry.name == path.name for entry in path.parent.iterdir())


def rename_case_variant_exactly(path: Path, temporary: Path, replacement: Path) -> None:
    if path.parent != temporary.parent or path.parent != replacement.parent:
        message = "Obsidian exact-name rename paths must share one directory."
        raise ValueError(message)
    if not paths_are_same_case_variant(path, replacement):
        message = f"Obsidian exact-name rename destination is not a safe alias: {replacement}"
        raise FileExistsError(message)
    if temporary.is_symlink() or temporary.exists() or path_entry_exists_exact(temporary):
        message = f"Obsidian exact-name rename temporary path is no longer free: {temporary}"
        raise FileExistsError(message)
    try:
        path.rename(temporary)
    except OSError:
        best_effort_restore_case_rename(temporary, path)
        raise
    try:
        if replacement.is_symlink() or replacement.exists() or path_entry_exists_exact(replacement):
            message = f"Obsidian exact-name rename destination is no longer free: {replacement}"
            raise FileExistsError(message)
        temporary.rename(replacement)
    except OSError:
        best_effort_restore_case_rename(temporary, path)
        raise


def best_effort_restore_case_rename(temporary: Path, original: Path) -> None:
    if (
        not path_entry_exists_exact(temporary)
        or original.exists()
        or path_entry_exists_exact(original)
    ):
        return
    with suppress(OSError):
        temporary.rename(original)


def portable_path_key(path: Path) -> str:
    return unicodedata.normalize("NFC", path.as_posix()).casefold()


def validate_obsidian_scan_directories(root_dir: Path) -> None:
    root = root_dir.resolve()
    for directory in OBSIDIAN_SCAN_DIRS:
        directory_path = root_dir / directory
        if directory_path.is_symlink():
            message = f"Obsidian snapshot directory must not be a symlink: {directory}"
            raise ValueError(message)
        if directory_path.exists() and not directory_path.is_dir():
            message = f"Obsidian snapshot directory is not a directory: {directory}"
            raise ValueError(message)
        if not directory_path.resolve().is_relative_to(root):
            message = f"Obsidian snapshot directory escapes configured root: {directory}"
            raise ValueError(message)


def managed_object_key(path: Path) -> str | None:
    identity = marker_identity_from_note_text(path.read_text(encoding="utf-8"))
    return identity.object_key if identity is not None else None


def object_key_from_note_text(text: str) -> str | None:
    identity = marker_identity_from_note_text(text)
    if identity is None or identity.marker_version != OBSIDIAN_MARKER_VERSION:
        return None
    return identity.object_key


def legacy_object_key_from_note_text(text: str) -> str | None:
    identity = marker_identity_from_note_text(text)
    if identity is None or identity.marker_version != LEGACY_OBSIDIAN_MARKER_VERSION:
        return None
    return identity.object_key


def marker_identity_from_note_text(text: str) -> _MarkerIdentity | None:
    managed_lines = [line for line in text.splitlines() if line.startswith("<!-- meetily-memory:")]
    if len(managed_lines) != 1:
        return None
    line = managed_lines[0]
    current_match = MANAGED_MARKER_RE.fullmatch(line)
    legacy_match = LEGACY_MANAGED_MARKER_RE.fullmatch(line)
    if current_match is not None:
        marker_version = OBSIDIAN_MARKER_VERSION
        encoded = current_match.group(1)
    elif legacy_match is not None:
        marker_version = LEGACY_OBSIDIAN_MARKER_VERSION
        encoded = legacy_match.group(1)
    else:
        return None
    try:
        padding = "=" * (-len(encoded) % 4)
        object_key = base64.urlsafe_b64decode(f"{encoded}{padding}").decode()
        payload = loads_json(object_key)
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    canonical_encoded = base64.urlsafe_b64encode(object_key.encode()).decode().rstrip("=")
    if canonical_encoded != encoded or not is_canonical_marker_payload(
        object_key,
        payload,
        marker_version,
    ):
        return None
    return _MarkerIdentity(marker_version, object_key, cast("dict[object, object]", payload))


def is_canonical_marker_payload(object_key: str, payload: object, marker_version: int) -> bool:
    if marker_version == OBSIDIAN_MARKER_VERSION:
        valid = is_valid_identity_payload(payload)
    elif marker_version == LEGACY_OBSIDIAN_MARKER_VERSION:
        valid = is_valid_legacy_identity_payload(payload)
    else:
        return False
    return valid and dumps_json(payload) == object_key


def is_canonical_object_key(object_key: str, *, payload: object | None = None) -> bool:
    try:
        decoded = loads_json(object_key) if payload is None else payload
    except ValueError:
        return False
    return is_valid_identity_payload(decoded) and dumps_json(decoded) == object_key


def is_valid_identity_payload(payload: object) -> bool:
    return is_valid_kind_payload(payload, OBJECT_KEY_VERSION, IDENTITY_SCHEMAS)


def is_valid_legacy_identity_payload(payload: object) -> bool:
    if not is_valid_kind_payload(payload, LEGACY_OBJECT_KEY_VERSION, LEGACY_IDENTITY_SCHEMAS):
        return False
    identity = cast("dict[object, object]", payload)
    return identity["kind"] != "entity" or identity["entity_kind"] in ENTITY_DIRS


def is_valid_kind_payload(
    payload: object,
    version: int,
    schemas: dict[str, frozenset[str]],
) -> bool:
    if type(payload) is not dict:
        return False
    identity = cast("dict[object, object]", payload)
    if type(identity.get("version")) is not int or identity["version"] != version:
        return False
    kind = identity.get("kind")
    if type(kind) is not str or kind not in schemas:
        return False
    if set(identity) != schemas[kind]:
        return False
    string_fields = schemas[kind] - {"version"}
    return not any(
        type(identity.get(field)) is not str or not identity[field] for field in string_fields
    )


def has_managed_marker(path: Path) -> bool:
    return managed_object_key(path) is not None
