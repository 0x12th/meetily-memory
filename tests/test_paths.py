from pathlib import Path

from meetily_memory.config.paths import candidate_meetily_db_paths, discover_meetily_db


def test_discovery_prefers_environment_source(tmp_path: Path) -> None:
    source = tmp_path / "custom" / "meeting_minutes.sqlite"
    source.parent.mkdir()
    source.touch()
    env = {"MEETILY_MEMORY_SOURCE": str(source)}

    assert discover_meetily_db(env=env, home=tmp_path) == source
    assert candidate_meetily_db_paths(env=env, home=tmp_path)[0] == source


def test_discovery_candidates_are_cross_platform(tmp_path: Path) -> None:
    env = {
        "XDG_DATA_HOME": str(tmp_path / "xdg-data"),
        "APPDATA": str(tmp_path / "AppData" / "Roaming"),
    }

    paths = [str(path) for path in candidate_meetily_db_paths(env=env, home=tmp_path)]

    assert any("com.meetily.ai" in path for path in paths)
    assert any("meeting_minutes.sqlite" in path for path in paths)
    assert any("meeting_minutes.db" in path for path in paths)
