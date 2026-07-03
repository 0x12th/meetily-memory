import sqlite3
from pathlib import Path

import pytest


@pytest.fixture
def meetily_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "meeting_minutes.sqlite"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE meetings (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            folder_path TEXT
        );

        CREATE TABLE transcripts (
            id TEXT PRIMARY KEY,
            meeting_id TEXT NOT NULL,
            transcript TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            summary TEXT,
            action_items TEXT,
            key_points TEXT,
            audio_start_time REAL,
            audio_end_time REAL,
            duration REAL,
            speaker TEXT,
            FOREIGN KEY (meeting_id) REFERENCES meetings(id)
        );

        CREATE TABLE summary_processes (
            meeting_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            error TEXT,
            result TEXT,
            start_time TEXT,
            end_time TEXT,
            chunk_count INTEGER DEFAULT 0,
            processing_time REAL DEFAULT 0.0,
            metadata TEXT
        );

        CREATE TABLE meeting_notes (
            meeting_id TEXT PRIMARY KEY NOT NULL,
            notes_markdown TEXT,
            notes_json TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.execute(
        """
        INSERT INTO meetings (id, title, created_at, updated_at, folder_path)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "meeting-1",
            "Launch Planning",
            "2026-07-01T10:00:00Z",
            "2026-07-01T11:00:00Z",
            str(tmp_path / "Launch Planning"),
        ),
    )
    conn.execute(
        """
        INSERT INTO meetings (id, title, created_at, updated_at, folder_path)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "meeting-2",
            "Vladimir Follow-up",
            "2026-07-02T09:00:00Z",
            "2026-07-02T09:30:00Z",
            str(tmp_path / "Vladimir Follow-up"),
        ),
    )
    conn.executemany(
        """
        INSERT INTO transcripts (
            id, meeting_id, transcript, timestamp, audio_start_time,
            audio_end_time, duration, speaker
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "transcript-1",
                "meeting-1",
                "Alice confirmed the launch checklist and pricing decision.",
                "10:05:00",
                300.0,
                315.0,
                15.0,
                "Alice",
            ),
            (
                "transcript-2",
                "meeting-2",
                "Vladimir agreed to send migration risks by Friday.",
                "09:10:00",
                600.0,
                620.0,
                20.0,
                "Vladimir",
            ),
            (
                "transcript-3",
                "meeting-1",
                "Open question: who owns partner review?",
                "10:15:00",
                900.0,
                910.0,
                10.0,
                "Alice",
            ),
        ],
    )
    conn.execute(
        """
        INSERT INTO summary_processes (
            meeting_id, status, created_at, updated_at, result, metadata
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "meeting-1",
            "completed",
            "2026-07-01T11:01:00Z",
            "2026-07-01T11:02:00Z",
            '{"markdown":"Launch checklist approved."}',
            '{"language":"en"}',
        ),
    )
    conn.execute(
        """
        INSERT INTO meeting_notes (
            meeting_id, notes_markdown, notes_json, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            "meeting-2",
            "Vladimir owns the migration risk list.",
            None,
            "2026-07-02T09:31:00Z",
            "2026-07-02T09:32:00Z",
        ),
    )
    conn.commit()
    conn.close()
    return db_path
