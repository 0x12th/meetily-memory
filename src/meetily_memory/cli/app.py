import os
from pathlib import Path
from typing import Annotated

import typer

from meetily_memory.cli.autosync_commands import autosync_app
from meetily_memory.cli.common import index_option, make_typer, version_callback
from meetily_memory.cli.lifecycle_commands import app as lifecycle_app
from meetily_memory.cli.lifecycle_commands import config_app, db_app, mcp_app
from meetily_memory.cli.llm_commands import app as llm_root_app
from meetily_memory.cli.llm_commands import llm_app
from meetily_memory.cli.obsidian_commands import obsidian_app
from meetily_memory.cli.search_commands import app as search_app
from meetily_memory.cli.semantic_commands import app as semantic_root_app
from meetily_memory.cli.semantic_commands import semantic_app
from meetily_memory.config.paths import app_config_path

app = make_typer(
    "Local search over Meetily meeting history.\n\n"
    "\b\n"
    "Main workflow:\n"
    "  mm s QUERY       Find meetings and source excerpts.\n"
    "  mm open ID       Open the original meeting."
)
app.add_typer(lifecycle_app)
app.add_typer(semantic_root_app)
app.add_typer(llm_root_app)
app.add_typer(search_app)
app.add_typer(semantic_app, name="semantic", hidden=True)
app.add_typer(llm_app, name="llm", hidden=True)
app.add_typer(obsidian_app, name="obsidian", hidden=True)
app.add_typer(autosync_app, name="autosync")
app.add_typer(config_app, name="config", hidden=True)
app.add_typer(db_app, name="db", hidden=True)
app.add_typer(mcp_app, name="mcp", hidden=True)


@app.callback()
def callback(
    ctx: typer.Context,
    index: Annotated[
        Path | None,
        typer.Option("--index", help="Path to Meetily Memory index.sqlite."),
    ] = None,
    version_output: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=version_callback,
            help="Show version and exit.",
            is_eager=True,
        ),
    ] = False,
) -> None:
    del version_output
    index_path = index_option(index)
    explicit_data_dir = os.environ.get("MEETILY_MEMORY_DATA_DIR")
    settings_path = (
        app_config_path()
        if index is None or explicit_data_dir
        else index_path.with_name("settings.json")
    )
    ctx.obj = {"index_path": index_path, "settings_path": settings_path}


def main() -> None:
    app()


if __name__ == "__main__":
    main()
