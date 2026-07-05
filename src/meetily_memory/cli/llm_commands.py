from typing import Annotated
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import typer

from meetily_memory.cli.common import core_from_context, make_typer, print_json, print_text_block
from meetily_memory.config.settings import LLMSettings, load_app_settings, update_app_settings
from meetily_memory.context_builder import DEFAULT_CONTEXT_LIMIT
from meetily_memory.json_codec import dumps_json, loads_json
from meetily_memory.semantic_search import DEFAULT_OLLAMA_URL

app = make_typer("Optional LLM answer commands.")
llm_app = make_typer("Configure optional local LLM providers.")


@llm_app.command("init")
def llm_init(
    provider: Annotated[
        str,
        typer.Option("--provider", help="LLM provider: manual or ollama."),
    ] = "manual",
    model: Annotated[
        str | None,
        typer.Option("--model", help="Ollama model name."),
    ] = None,
    ollama_url: Annotated[
        str,
        typer.Option("--ollama-url", help="Ollama base URL."),
    ] = DEFAULT_OLLAMA_URL,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False,
) -> None:
    normalized_provider = normalize_llm_provider(provider)
    settings = update_app_settings(
        llm=LLMSettings(
            provider=normalized_provider,
            model=model or ("llama3.1" if normalized_provider == "ollama" else None),
            ollama_url=ollama_url if normalized_provider == "ollama" else None,
        )
    )
    payload = settings.llm.__dict__
    if json_output:
        print_json(payload)
        return
    print_text_block(f"llm provider: {settings.llm.provider}")
    if settings.llm.model:
        print_text_block(f"model: {settings.llm.model}")
    if settings.llm.ollama_url:
        print_text_block(f"ollama url: {settings.llm.ollama_url}")


@app.command("ask", hidden=True)
def ask(
    ctx: typer.Context,
    question: str,
    meeting: Annotated[
        str | None,
        typer.Option("--meeting", help="Restrict context to one meeting."),
    ] = None,
    topic: Annotated[
        str | None,
        typer.Option("--topic", help="Ask against topic memory."),
    ] = None,
    limit: Annotated[int, typer.Option("--limit", "-n")] = DEFAULT_CONTEXT_LIMIT,
) -> None:
    settings = load_app_settings()
    provider = settings.llm.provider or "manual"
    prompt = build_ask_prompt(ctx, question, meeting=meeting, topic=topic, limit=limit)
    if provider == "manual":
        print_text_block("# Manual LLM Prompt")
        print_text_block("")
        print_text_block(prompt)
        return
    if provider == "ollama":
        print_text_block(call_ollama_generate(settings.llm, prompt))
        return
    message = f"Unknown LLM provider: {provider}"
    raise typer.BadParameter(message)


def normalize_llm_provider(provider: str) -> str:
    value = provider.casefold()
    if value in {"manual", "ollama"}:
        return value
    message = "Unknown LLM provider. Use `manual` or `ollama`."
    raise typer.BadParameter(message)


def build_ask_prompt(
    ctx: typer.Context,
    question: str,
    *,
    meeting: str | None,
    topic: str | None,
    limit: int,
) -> str:
    core = core_from_context(ctx)
    retrieval_question = question
    topic_payload: dict[str, object] | None = None
    if topic:
        topic_payload = core.topic(topic, limit).data
        retrieval_question = f"{topic} {question}"
    try:
        context_payload = (
            core.build_meeting_context(retrieval_question, meeting, limit).data
            if meeting
            else core.build_context(retrieval_question, limit).data
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    lines = [
        "Answer the question using only the source-backed context below.",
        "If the context is insufficient, say what is missing.",
        "",
        f"Question: {question}",
    ]
    if meeting:
        lines.append(f"Meeting: {meeting}")
    if topic:
        lines.append(f"Topic: {topic}")
    if topic_payload:
        lines.extend(["", render_topic_memory_for_prompt(topic_payload)])
    lines.extend(["", str(context_payload["markdown"])])
    return "\n".join(lines).rstrip() + "\n"


def render_topic_memory_for_prompt(topic_payload: dict[str, object]) -> str:
    topic = topic_payload.get("topic")
    topic_title = topic.get("title") if isinstance(topic, dict) else "unknown"
    lines = ["# Topic Memory", "", f"Topic: {topic_title}"]
    meetings = topic_payload.get("meetings")
    if isinstance(meetings, list) and meetings:
        lines.extend(["", "## Meetings"])
        for meeting in meetings[:10]:
            if not isinstance(meeting, dict):
                continue
            external_id = meeting.get("meeting_external_id") or meeting.get("external_id")
            lines.append(f"- {meeting.get('title')} [{external_id}]")
    signals = topic_payload.get("structured_signals")
    if isinstance(signals, list) and signals:
        lines.extend(["", "## Structured Signals"])
        for signal in signals[:20]:
            if not isinstance(signal, dict):
                continue
            source = " / ".join(
                str(part)
                for part in (
                    signal.get("meeting_external_id"),
                    signal.get("chunk_external_id") or signal.get("source_chunk_id"),
                )
                if part
            )
            lines.append(f"- {signal.get('kind')}: {signal.get('text')} (Source: {source})")
    people = topic_payload.get("related_people")
    if isinstance(people, list) and people:
        lines.extend(["", "## People"])
        lines.extend(
            f"- {person['display_name']}"
            for person in people[:20]
            if isinstance(person, dict) and person.get("display_name")
        )
    return "\n".join(lines).rstrip()


def call_ollama_generate(settings: LLMSettings, prompt: str) -> str:
    model = settings.model or "llama3.1"
    base_url = settings.ollama_url or DEFAULT_OLLAMA_URL
    endpoint = f"{base_url.rstrip('/')}/api/generate"
    payload = dumps_json({"model": model, "prompt": prompt, "stream": False}).encode()
    request = Request(  # noqa: S310
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=120) as response:  # noqa: S310
            data = response.read().decode()
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        message = (
            "Ollama LLM provider is unavailable. Start Ollama or run "
            "`mm llm init --provider manual`."
        )
        raise typer.BadParameter(message) from exc
    payload_obj = loads_json(data)
    if not isinstance(payload_obj, dict) or not isinstance(payload_obj.get("response"), str):
        message = "Unexpected Ollama response."
        raise typer.BadParameter(message)
    return str(payload_obj["response"])
