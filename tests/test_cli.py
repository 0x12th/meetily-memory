from pathlib import Path

from typer.testing import CliRunner

from meetily_memory.cli.app import app


def test_cli_v1_scan_search_list_last_person_and_doctor(meetily_db: Path, tmp_path: Path) -> None:
    index_path = tmp_path / "index.sqlite"
    runner = CliRunner()

    scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db)],
    )
    assert scan.exit_code == 0
    assert "meetings seen: 2" in scan.stdout

    force_scan = runner.invoke(
        app,
        ["--index", str(index_path), "scan", "--source", str(meetily_db), "--force"],
    )
    assert force_scan.exit_code == 0
    assert "meetings updated: 2" in force_scan.stdout

    search = runner.invoke(app, ["--index", str(index_path), "s", "pricing decision"])
    assert search.exit_code == 0
    assert "Launch Planning" in search.stdout
    assert "pricing decision" in search.stdout

    listing = runner.invoke(app, ["--index", str(index_path), "ls"])
    assert listing.exit_code == 0
    assert "Robert Follow-up" in listing.stdout
    assert "Launch Planning" in listing.stdout

    last_for_person = runner.invoke(app, ["--index", str(index_path), "last", "--person", "Robert"])
    assert last_for_person.exit_code == 0
    assert "Robert Follow-up" in last_for_person.stdout

    person = runner.invoke(app, ["--index", str(index_path), "p", "Robert"])
    assert person.exit_code == 0
    assert "Robert Follow-up" in person.stdout

    doctor = runner.invoke(
        app,
        ["--index", str(index_path), "doctor", "--source", str(meetily_db)],
    )
    assert doctor.exit_code == 0
    assert "source readable: yes" in doctor.stdout
    assert "fts5: yes" in doctor.stdout

    opened = runner.invoke(app, ["--index", str(index_path), "open", "meeting-2", "--print-path"])
    assert opened.exit_code == 0
    assert "Robert Follow-up" in opened.stdout
