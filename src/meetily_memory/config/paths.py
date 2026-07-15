import os
from collections.abc import Mapping
from pathlib import Path

from platformdirs import user_data_dir


def default_data_dir() -> Path:
    env_data_dir = os.environ.get("MEETILY_MEMORY_DATA_DIR")
    if env_data_dir:
        return Path(env_data_dir).expanduser()
    return Path(user_data_dir("meetily-memory", appauthor=False))


def default_index_path() -> Path:
    return default_data_dir() / "index.sqlite"


def default_state_path() -> Path:
    return default_data_dir() / "state.sqlite"


def semantic_config_path() -> Path:
    return default_data_dir() / "config.json"


def app_config_path() -> Path:
    return default_data_dir() / "settings.json"


def candidate_meetily_db_paths(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> list[Path]:
    env = env or os.environ
    home = home or Path.home()
    candidates: list[Path] = []
    env_source = env.get("MEETILY_MEMORY_SOURCE")
    if env_source:
        candidates.append(Path(env_source).expanduser())

    candidates.extend(platform_data_candidates(env))
    candidates.extend(
        [
            home / ".config" / "Meetily" / "meeting_minutes.sqlite",
            home / ".config" / "meetily" / "meeting_minutes.sqlite",
            home / ".local" / "share" / "Meetily" / "meeting_minutes.sqlite",
            home / ".local" / "share" / "meetily" / "meeting_minutes.sqlite",
            home / "AppData" / "Roaming" / "com.meetily.ai" / "meeting_minutes.sqlite",
            home / "AppData" / "Roaming" / "meetily" / "meeting_minutes.sqlite",
        ]
    )

    mac_legacy = [
        home / "Library" / "Application Support" / "com.meetily.ai" / "meeting_minutes.sqlite",
        home / "Library" / "Application Support" / "com.meetily.ai" / "meeting_minutes.db",
        home / "Library" / "Application Support" / "meetily" / "meeting_minutes.sqlite",
        home / "Library" / "Application Support" / "Meetily" / "meeting_minutes.sqlite",
    ]
    candidates.extend(mac_legacy)
    return dedupe_paths(candidates)


def platform_data_candidates(env: Mapping[str, str]) -> list[Path]:
    candidates: list[Path] = []
    app_names = ("com.meetily.ai", "meetily", "Meetily")

    xdg_data_home = env.get("XDG_DATA_HOME")
    if xdg_data_home:
        for app_name in app_names:
            candidates.extend(db_files(Path(xdg_data_home) / app_name))

    appdata = env.get("APPDATA")
    if appdata:
        for app_name in app_names:
            candidates.extend(db_files(Path(appdata) / app_name))

    for app_name in app_names:
        candidates.extend(db_files(Path(user_data_dir(app_name, appauthor=False))))
    return candidates


def db_files(directory: Path) -> list[Path]:
    return [
        directory / "meeting_minutes.sqlite",
        directory / "meeting_minutes.db",
    ]


def discover_meetily_db(
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path | None:
    for path in candidate_meetily_db_paths(env=env, home=home):
        if path.is_file():
            return path
    return None


def dedupe_paths(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    deduped: list[Path] = []
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped
