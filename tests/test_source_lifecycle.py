import json
import shutil
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from meetily_memory.cli.app import app
from meetily_memory.core import CORE_V2_VERSION, MeetilyMemoryCore
from meetily_memory.json_codec import loads_json


def test_legacy_source_path_migrates_idempotently_to_selected_uuid(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    index_path = data_dir / "index.sqlite"
    settings_path = data_dir / "settings.json"
    settings_path.write_text(json.dumps({"source_path": str(meetily_db)}) + "\n")
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}

    first = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    first_settings = loads_json(settings_path.read_text())
    second = runner.invoke(app, ["--index", str(index_path), "status", "--json"], env=env)
    second_settings = loads_json(settings_path.read_text())

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert first_settings["source_uuid"] == second_settings["source_uuid"]
    assert "source_path" not in first_settings
    assert json.loads(second.stdout)["source_path"] == str(meetily_db)
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1


def test_explicit_rebind_preserves_identity_evidence_and_task_state(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    moved_db = tmp_path / "moved" / "meeting_minutes.sqlite"
    moved_db.parent.mkdir()
    shutil.copyfile(meetily_db, moved_db)
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}

    init = runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db), "--no-autosync"],
        env=env,
    )
    assert init.exit_code == 0
    before = MeetilyMemoryCore(index_path)
    evidence_id = before.search("migration risks", limit=1, contract_version=CORE_V2_VERSION).data[
        "results"
    ][0]["id"]
    task = before.repo.list_structured_entity_details("action_items")[0]
    before.set_task_status(task["id"], "done", note="survives move")
    original_uuid = loads_json((data_dir / "settings.json").read_text())["source_uuid"]

    rebind = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(moved_db), "--rebind"],
        env=env,
    )
    scan = runner.invoke(app, ["--index", str(index_path), "scan"], env=env)

    assert rebind.exit_code == 0
    assert f"old source path: {meetily_db}" in rebind.stdout
    assert f"new source path: {moved_db}" in rebind.stdout
    assert "matching meetings: 2" in rebind.stdout
    assert scan.exit_code == 0
    settings = loads_json((data_dir / "settings.json").read_text())
    assert settings["source_uuid"] == original_uuid
    assert "source_path" not in settings
    after = MeetilyMemoryCore(index_path)
    assert (
        after.search("migration risks", limit=1, contract_version=CORE_V2_VERSION).data["results"][
            0
        ]["id"]
        == evidence_id
    )
    matching_tasks = [
        row
        for row in after.repo.list_structured_entity_details("action_items", limit=100)
        if row["text"] == task["text"]
    ]
    assert matching_tasks[0]["status"] == "done"
    assert matching_tasks[0]["status_note"] == "survives move"
    with sqlite3.connect(index_path) as conn:
        sources = conn.execute("SELECT path FROM sources").fetchall()
    assert sources == [(str(moved_db),)]


def test_source_selection_without_rebind_uses_a_distinct_uuid(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    other_db = tmp_path / "other.sqlite"
    shutil.copyfile(meetily_db, other_db)
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db), "--no-autosync"],
        env=env,
    )
    old_uuid = loads_json((data_dir / "settings.json").read_text())["source_uuid"]

    selected = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(other_db)],
        env=env,
    )

    assert selected.exit_code == 0
    new_uuid = loads_json((data_dir / "settings.json").read_text())["source_uuid"]
    assert new_uuid != old_uuid
    assert "source_path" not in loads_json((data_dir / "settings.json").read_text())


def test_incompatible_rebind_is_atomic_and_creates_no_new_source(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    incompatible_db = tmp_path / "incompatible.sqlite"
    shutil.copyfile(meetily_db, incompatible_db)
    with sqlite3.connect(incompatible_db) as conn:
        conn.execute("UPDATE meetings SET id = 'other-' || id")
        conn.execute("UPDATE transcripts SET meeting_id = 'other-' || meeting_id")
        conn.execute("UPDATE summary_processes SET meeting_id = 'other-' || meeting_id")
        conn.execute("UPDATE meeting_notes SET meeting_id = 'other-' || meeting_id")
        conn.commit()
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db), "--no-autosync"],
        env=env,
    )
    original_settings = loads_json((data_dir / "settings.json").read_text())

    rebind = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(incompatible_db), "--rebind"],
        env=env,
    )

    assert rebind.exit_code != 0
    assert "no matching meeting IDs" in rebind.output
    assert loads_json((data_dir / "settings.json").read_text()) == original_settings
    with sqlite3.connect(data_dir / "state.sqlite") as conn:
        assert conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert conn.execute("SELECT current_path FROM sources").fetchone()[0] == str(meetily_db)


def test_rebind_accepts_a_partial_copy_with_one_matching_meeting(
    meetily_db: Path, tmp_path: Path
) -> None:
    data_dir = tmp_path / "data"
    index_path = data_dir / "index.sqlite"
    partial_db = tmp_path / "partial.sqlite"
    shutil.copyfile(meetily_db, partial_db)
    with sqlite3.connect(partial_db) as conn:
        conn.execute("DELETE FROM transcripts WHERE meeting_id = 'meeting-2'")
        conn.execute("DELETE FROM meeting_notes WHERE meeting_id = 'meeting-2'")
        conn.execute("DELETE FROM meetings WHERE id = 'meeting-2'")
        conn.commit()
    runner = CliRunner()
    env = {"MEETILY_MEMORY_DATA_DIR": str(data_dir)}
    runner.invoke(
        app,
        ["--index", str(index_path), "init", "--source", str(meetily_db), "--no-autosync"],
        env=env,
    )

    rebind = runner.invoke(
        app,
        ["--index", str(index_path), "config", "source", str(partial_db), "--rebind"],
        env=env,
    )

    assert rebind.exit_code == 0
    assert "matching meetings: 1" in rebind.stdout
