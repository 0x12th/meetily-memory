from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from meetily_memory.repositories.index import IndexRepository
from meetily_memory.scanner.meetily_sqlite import (
    extract_language,
    fingerprint_json,
    normalize_meeting,
)
from tests.index_helpers import publish_fresh_index


@pytest.mark.parametrize(
    ("summary_process", "expected"),
    [
        pytest.param(None, None, id="summary-process-absent"),
        pytest.param({}, None, id="metadata-absent"),
        pytest.param({"metadata": ""}, None, id="metadata-empty"),
        pytest.param({"metadata": "{"}, None, id="metadata-malformed"),
        pytest.param({"metadata": "null"}, None, id="metadata-null"),
        pytest.param({"metadata": "[]"}, None, id="metadata-array"),
        pytest.param({"metadata": "42"}, None, id="metadata-number"),
        pytest.param({"metadata": '"en"'}, None, id="metadata-string"),
        pytest.param({"metadata": "{}"}, None, id="metadata-empty-object"),
        pytest.param({"metadata": '{"language":42}'}, None, id="language-non-string"),
        pytest.param({"metadata": '{"language":""}'}, None, id="language-empty-string"),
        pytest.param(
            {"metadata": '{"language":"   "}'},
            None,
            id="language-whitespace-only",
        ),
        pytest.param({"metadata": '{"language":"en"}'}, "en", id="language-valid"),
    ],
)
def test_extract_language_accepts_only_non_empty_strings_from_objects(
    summary_process: dict[str, Any] | None,
    expected: str | None,
) -> None:
    assert extract_language(summary_process) == expected


def test_normalize_meeting_assigns_retained_chunks_contiguous_global_ordinals() -> None:
    upstream = _upstream_meeting(
        [
            {"id": "blank-leading", "transcript": "  "},
            {"id": "transcript-first", "transcript": "First retained transcript."},
            {"id": "blank-middle", "transcript": "\r\n"},
            {"id": "transcript-second", "transcript": "Second retained transcript."},
            {"id": "blank-trailing", "transcript": ""},
        ]
    )

    _, chunks = normalize_meeting(Path("source.sqlite"), upstream, "2026-08-28T00:00:00Z")

    assert [(chunk.kind, chunk.external_id, chunk.ordinal, chunk.text) for chunk in chunks] == [
        ("transcript", "transcript-first", 0, "First retained transcript."),
        ("transcript", "transcript-second", 1, "Second retained transcript."),
        ("summary", "summary:meeting-edge", 2, "Retained summary."),
        ("note", "note:meeting-edge", 3, "Retained note."),
    ]
    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))


def test_contiguous_order_changes_the_pre_fix_meeting_fingerprint() -> None:
    upstream = _upstream_meeting(
        [
            {"id": "transcript-first", "transcript": "First retained transcript."},
            {"id": "blank-middle", "transcript": " "},
            {"id": "transcript-second", "transcript": "Second retained transcript."},
        ]
    )

    meeting, chunks = normalize_meeting(
        Path("source.sqlite"),
        upstream,
        "2026-08-28T00:00:00Z",
    )
    previous_fingerprint = fingerprint_json(
        {
            "meeting": {
                "id": upstream.get("id"),
                "title": upstream.get("title"),
                "created_at": upstream.get("created_at"),
                "updated_at": upstream.get("updated_at"),
                "folder_path": upstream.get("folder_path"),
            },
            "chunks": [chunk.fingerprint for chunk in chunks],
            "summary": upstream.get("summary_process"),
            "notes": upstream.get("notes"),
        }
    )

    assert meeting.fingerprint != previous_fingerprint


def test_normalize_meeting_starts_summary_and_note_at_zero_when_transcripts_are_blank() -> None:
    upstream = _upstream_meeting(
        [
            {"id": "blank-leading", "transcript": ""},
            {"id": "blank-trailing", "transcript": "   \n"},
        ]
    )

    _, chunks = normalize_meeting(Path("source.sqlite"), upstream, "2026-08-28T00:00:00Z")

    assert [(chunk.kind, chunk.ordinal) for chunk in chunks] == [("summary", 0), ("note", 1)]


def test_fresh_scan_handles_non_object_language_metadata(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as connection:
        connection.execute(
            "UPDATE summary_processes SET metadata = ? WHERE meeting_id = ?",
            ("[]", "meeting-1"),
        )
        connection.commit()

    index_path = tmp_path / "index.sqlite"
    result = publish_fresh_index(index_path, meetily_db)
    repository = IndexRepository.open_existing(index_path)
    meeting_ref = repository.meeting_ref_for_local_id(1)
    assert meeting_ref is not None
    meeting = repository.get_meeting_by_ref(meeting_ref)

    assert result.meetings == 2
    assert meeting is not None
    assert meeting["language"] is None


def test_fresh_rescan_preserves_context_order_and_fallback_evidence(
    meetily_db: Path,
    tmp_path: Path,
) -> None:
    with sqlite3.connect(meetily_db) as connection:
        connection.executemany(
            """
            INSERT INTO transcripts (
                id, meeting_id, transcript, timestamp, audio_start_time,
                audio_end_time, duration, speaker
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("blank-leading", "meeting-1", " ", "10:00:00", 0.0, 1.0, 1.0, None),
                ("blank-middle", "meeting-1", "\r\n", "10:10:00", 600.0, 601.0, 1.0, None),
                (
                    None,
                    "meeting-1",
                    "Fallback evidence marker.",
                    "10:12:00",
                    720.0,
                    721.0,
                    1.0,
                    "Alice",
                ),
                ("blank-trailing", "meeting-1", "", "10:20:00", 1200.0, 1201.0, 1.0, None),
            ],
        )
        connection.execute(
            """
            INSERT INTO meeting_notes (
                meeting_id, notes_markdown, notes_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "meeting-1",
                "Retained note.",
                None,
                "2026-07-01T11:03:00Z",
                "2026-07-01T11:04:00Z",
            ),
        )
        connection.commit()

    index_path = tmp_path / "index.sqlite"
    publish_fresh_index(index_path, meetily_db)
    repository = IndexRepository.open_existing(index_path)
    meeting_ref = repository.meeting_ref_for_local_id(1)
    assert meeting_ref is not None
    meeting = repository.get_meeting_by_ref(meeting_ref)
    assert meeting is not None
    chunks = repository.get_chunks_for_meeting(int(meeting["id"]))
    first_hit = repository.search_hits("fallback evidence marker", limit=1)[0]

    assert [(chunk["kind"], chunk["external_id"], chunk["ordinal"]) for chunk in chunks] == [
        ("transcript", "transcript-1", 0),
        ("transcript", None, 1),
        ("transcript", "transcript-3", 2),
        ("summary", "summary:meeting-1", 3),
        ("note", "note:meeting-1", 4),
    ]
    assert first_hit.excerpt.chunk_external_id is None
    assert first_hit.excerpt.ordinal == 1

    publish_fresh_index(index_path, meetily_db)
    reopened = IndexRepository.open_existing(index_path)
    second_hit = reopened.search_hits("fallback evidence marker", limit=1)[0]

    assert second_hit.excerpt.ordinal == first_hit.excerpt.ordinal
    assert second_hit.id == first_hit.id


def _upstream_meeting(transcripts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "id": "meeting-edge",
        "title": "Scanner edge cases",
        "created_at": "2026-08-28T00:00:00Z",
        "updated_at": "2026-08-28T01:00:00Z",
        "folder_path": None,
        "transcripts": transcripts,
        "summary_process": {
            "result": '{"markdown":"Retained summary."}',
            "metadata": '{"language":"en"}',
        },
        "notes": {"notes_markdown": "Retained note."},
    }
