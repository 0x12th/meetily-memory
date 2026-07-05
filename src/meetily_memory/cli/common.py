import sqlite3
import subprocess
import sys
from contextlib import closing
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from shutil import which

import typer
from rich.console import Console
from rich.table import Table

from meetily_memory import __version__ as fallback_version
from meetily_memory.config.paths import default_index_path
from meetily_memory.core import MeetilyMemoryCore
from meetily_memory.json_codec import dumps_json

PACKAGE_NAME = "meetily-memory"
console = Console()

ENTITY_COMMANDS = {
    "decisions": "decisions",
    "tasks": "action_items",
    "risks": "risks",
    "questions": "open_questions",
}
ENTITY_LABELS = {
    "decisions": "Decisions",
    "action_items": "Action items",
    "risks": "Risks",
    "open_questions": "Open questions",
}


def make_typer(help_text: str) -> typer.Typer:
    return typer.Typer(
        no_args_is_help=True,
        help=help_text,
        add_completion=False,
        pretty_exceptions_enable=False,
        rich_markup_mode=None,
        suggest_commands=False,
    )


def index_option(index: Path | None) -> Path:
    return index or default_index_path()


def package_version() -> str:
    try:
        return version(PACKAGE_NAME)
    except PackageNotFoundError:
        return fallback_version


def version_callback(value: bool) -> None:  # noqa: FBT001
    if value:
        print_text_block(f"{PACKAGE_NAME} {package_version()}")
        raise typer.Exit


def print_json(payload: object) -> None:
    sys.stdout.write(dumps_json(payload))
    sys.stdout.write("\n")


def print_text_block(text: str) -> None:
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")


def core_from_context(ctx: typer.Context) -> MeetilyMemoryCore:
    return MeetilyMemoryCore(ctx.obj["index_path"])


def meeting_label(row: dict[str, object]) -> str:
    date = compact_date(row.get("updated_at") or row.get("created_at"))
    suffix = f" ({date})" if date else ""
    return f"#{row['id']} {row['title']}{suffix}"


def print_meeting_table(rows: list[dict[str, object]]) -> None:
    table = Table("id", "date", "chunks", "open", "title")
    for row in rows:
        table.add_row(
            str(row["id"]),
            compact_date(row.get("updated_at") or row.get("created_at")),
            str(row["chunk_count"]),
            f"mm open {row['id']}",
            str(row["title"]),
        )
    console.print(table)


def compact_date(value: object) -> str:
    return str(value or "")[:10]


def open_path(path: Path) -> None:
    if not path.exists():
        message = f"Path does not exist: {path}"
        raise typer.BadParameter(message)
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    if which(opener) is None:
        message = f"Path opener was not found: {opener}"
        raise typer.BadParameter(message)
    result = subprocess.run([opener, str(path)], check=False)
    if result.returncode != 0:
        message = f"Could not open path: {path}"
        raise typer.BadParameter(message)


def sqlite_has_fts5() -> bool:
    with closing(sqlite3.connect(":memory:")) as conn:
        try:
            conn.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        except sqlite3.Error:
            return False
        return True
