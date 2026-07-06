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

app = make_typer(
    "Local Meetily history index.\n\n"
    "Everyday: s finds evidence, open verifies the source folder, c builds paste-ready "
    "LLM context.\n"
    "Stable: init, refresh, status, doctor, update, config, s, c, and open.\n"
    "Experimental: t/topic summarizes source-backed topic evidence; semantic, llm, "
    "Obsidian, autosync, db, and mcp remain available as opt-in integrations."
)
app.add_typer(lifecycle_app)
app.add_typer(semantic_root_app)
app.add_typer(llm_root_app)
app.add_typer(search_app)
app.add_typer(semantic_app, name="semantic")
app.add_typer(llm_app, name="llm")
app.add_typer(obsidian_app, name="obsidian")
app.add_typer(autosync_app, name="autosync")
app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(mcp_app, name="mcp")


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
    ctx.obj = {"index_path": index_option(index)}


def main() -> None:
    app()


if __name__ == "__main__":
    main()
