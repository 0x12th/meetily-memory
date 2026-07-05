from typing import Annotated

import typer

from meetily_memory.cli.common import (
    compact_date,
    console,
    make_typer,
    print_json,
    print_text_block,
)
from meetily_memory.cli.renderers import embedding_label, float_value
from meetily_memory.semantic_search import (
    DEFAULT_OLLAMA_MODEL,
    DEFAULT_OLLAMA_URL,
    SemanticSearchConfig,
    index_semantic_embeddings,
    load_semantic_config,
    resolve_embedding_provider,
    save_semantic_config,
    semantic_search,
)

app = make_typer("Semantic search root aliases.")
semantic_app = make_typer("Experimental semantic search commands.")


def semantic_search_command(
    ctx: typer.Context,
    query: Annotated[str, typer.Argument(help="Search query.")],
    limit: Annotated[int, typer.Option("--limit", "-n")] = 10,
    embedding_provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            "--embedding-provider",
            help="Embedding provider: ollama or hash.",
        ),
    ] = None,
    embedding_model: Annotated[
        str | None,
        typer.Option("--model", "--embedding-model", help="Ollama embedding model name."),
    ] = None,
    ollama_url: Annotated[
        str | None,
        typer.Option("--ollama-url", help="Ollama base URL."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        provider = resolve_embedding_provider(
            embedding_provider,
            ollama_model=embedding_model,
            ollama_url=ollama_url,
        )
        results = semantic_search(ctx.obj["index_path"], query, limit, embedding_provider=provider)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if json_output:
        print_json(results)
        return
    if not results:
        console.print("No semantic matches found. Run `mm scan` first.")
        return
    for result in results:
        date = compact_date(result.get("updated_at") or result.get("created_at"))
        suffix = f" ({date})" if date else ""
        console.print(f"#{result['meeting_id']} {result['title']}{suffix}")
        distance = float_value(result["distance"], "semantic distance")
        console.print(
            f"semantic distance: {distance:.4f} | "
            f"embedding: {embedding_label(result, provider)} | "
            f"open: mm open {result['meeting_id']}"
        )
        source_parts = [f"chunk #{result['chunk_id']}"]
        if result.get("timestamp_label"):
            source_parts.insert(0, str(result["timestamp_label"]))
        console.print(" | ".join(source_parts))
        console.print(result["text"])
        console.print()


semantic_app.command("search")(semantic_search_command)
app.command("sem")(semantic_search_command)


@semantic_app.command("init")
def semantic_init_command(
    provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            "--embedding-provider",
            help="Embedding provider: ollama or hash.",
        ),
    ] = None,
    model: Annotated[
        str | None,
        typer.Option("--model", "--embedding-model", help="Ollama embedding model name."),
    ] = None,
    ollama_url: Annotated[
        str | None,
        typer.Option("--ollama-url", help="Ollama base URL."),
    ] = None,
    show: Annotated[
        bool,
        typer.Option("--show", help="Show semantic search setup."),
    ] = False,
) -> None:
    semantic_setup(provider, model, ollama_url, show=show)


@semantic_app.command("index")
def semantic_index_command(
    ctx: typer.Context,
    embedding_provider: Annotated[
        str | None,
        typer.Option(
            "--provider",
            "--embedding-provider",
            help="Embedding provider: ollama or hash.",
        ),
    ] = None,
    embedding_model: Annotated[
        str | None,
        typer.Option("--model", "--embedding-model", help="Ollama embedding model name."),
    ] = None,
    ollama_url: Annotated[
        str | None,
        typer.Option("--ollama-url", help="Ollama base URL."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    try:
        provider = resolve_embedding_provider(
            embedding_provider,
            ollama_model=embedding_model,
            ollama_url=ollama_url,
        )
        indexed = index_semantic_embeddings(ctx.obj["index_path"], embedding_provider=provider)
    except RuntimeError as exc:
        raise typer.BadParameter(str(exc)) from exc
    payload = {
        "embedding_provider": provider.name,
        "embedding_model": provider.model,
        "embeddings_indexed": indexed,
    }
    if json_output:
        print_json(payload)
        return
    print_text_block(f"embedding: {provider.name}/{provider.model}")
    print_text_block(f"embeddings indexed: {indexed}")


def semantic_setup(
    provider: str | None,
    model: str | None,
    ollama_url: str | None,
    *,
    show: bool,
) -> None:
    existing = load_semantic_config()
    if show and provider is None and model is None and ollama_url is None:
        print_semantic_config(existing)
        return

    normalized_provider = normalize_semantic_provider(provider or existing.provider or "ollama")
    existing_model = existing.ollama_model if existing.provider == normalized_provider else None
    existing_ollama_url = existing.ollama_url if existing.provider == normalized_provider else None
    configured_ollama_url = None
    if normalized_provider == "ollama":
        configured_ollama_url = ollama_url or existing_ollama_url or DEFAULT_OLLAMA_URL
    config = SemanticSearchConfig(
        provider=normalized_provider,
        ollama_model=model or existing_model or default_model_for_provider(normalized_provider),
        ollama_url=configured_ollama_url,
    )
    config_path = save_semantic_config(config)
    print_semantic_config(config)
    print_text_block(f"config path: {config_path}")


def normalize_semantic_provider(provider: str) -> str:
    value = provider.casefold()
    if value in {"hash", "local-hash", "diagnostic"}:
        return "hash"
    if value == "ollama":
        return "ollama"
    message = "Unknown embedding provider. Use `ollama` or `hash`."
    raise typer.BadParameter(message)


def default_model_for_provider(provider: str) -> str:
    if provider == "hash":
        return "local-hash-v1"
    return DEFAULT_OLLAMA_MODEL


def print_semantic_config(config: SemanticSearchConfig) -> None:
    provider = config.provider or "ollama"
    model = config.ollama_model or default_model_for_provider(provider)
    ollama_url = config.ollama_url or DEFAULT_OLLAMA_URL
    print_text_block(f"semantic provider: {provider}")
    print_text_block(f"model: {model}")
    if provider == "ollama":
        print_text_block(f"ollama url: {ollama_url}")
