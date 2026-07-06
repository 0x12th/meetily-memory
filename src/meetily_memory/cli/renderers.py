from typing import cast

from meetily_memory.cli.common import compact_date, print_text_block


def print_topic_memory(memory: dict[str, object]) -> None:
    topic = cast("dict[str, object]", memory["topic"])
    labels = topic_labels(str(memory.get("ui_language") or "en"))
    print_text_block(f"{labels['title']}: {topic['title']}")
    aliases = cast("list[str]", topic.get("aliases", []))
    for alias in aliases:
        print_text_block(f"alias: {alias}")
    evidence = cast(
        "list[dict[str, object]]",
        memory.get("evidence", memory.get("meetings", [])),
    )
    print_text_block(f"\n{labels['summary']}")
    print_topic_summary(memory, evidence)
    print_text_block(f"\n{labels['meetings']}")
    print_search_meeting_summaries(cast("list[dict[str, object]]", memory["meetings"]))
    print_text_block(labels["decisions"])
    print_topic_section(memory, evidence, "decisions", labels)
    print_text_block(labels["tasks"])
    open_tasks = [
        row
        for row in entity_rows_for_kind(memory, "action_items")
        if row.get("status", "open") in {"open", "unknown"}
    ]
    print_topic_section(memory, evidence, "action_items", labels, structured_rows=open_tasks)
    print_text_block(labels["risks"])
    print_topic_section(memory, evidence, "risks", labels)
    print_text_block(labels["questions"])
    print_topic_section(memory, evidence, "open_questions", labels)
    print_text_block(labels["evidence"])
    print_evidence_bullets(evidence)
    people = cast("list[dict[str, object]]", memory.get("related_people", []))
    if people:
        print_text_block(labels["people"])
        for person in people:
            print_text_block(f"- {person['display_name']}")


def entity_rows_for_kind(memory: dict[str, object], kind: str) -> list[dict[str, object]]:
    rows = cast("list[dict[str, object]]", memory.get("structured_signals", []))
    return [row for row in rows if row["kind"] == kind]


def topic_labels(language: str) -> dict[str, str]:
    if language.casefold().split("-", maxsplit=1)[0] == "ru":
        return {
            "title": "Что известно",
            "summary": "Кратко",
            "meetings": "Связанные встречи",
            "decisions": "Возможные решения",
            "tasks": "Возможные задачи",
            "risks": "Возможные риски",
            "questions": "Возможные вопросы",
            "evidence": "Подтверждающие фрагменты",
            "people": "Связанные люди",
            "no_decisions": "Подтвержденные решения не найдены.",
            "no_tasks": "Возможные задачи не найдены.",
            "no_risks": "Возможные риски не найдены.",
            "no_questions": "Возможные вопросы не найдены.",
            "no_evidence": "Подтверждающие фрагменты не найдены.",
            "no_topic": "Подтверждающие фрагменты по теме не найдены.",
            "summary_found": (
                "Найдено фрагментов: {excerpts}; встреч: {meetings}; запросы: {terms}."
            ),
        }
    return {
        "title": "What we know",
        "summary": "Summary",
        "meetings": "Related meetings",
        "decisions": "Possible decisions",
        "tasks": "Possible tasks",
        "risks": "Possible risks",
        "questions": "Possible questions",
        "evidence": "Supporting excerpts",
        "people": "Related people",
        "no_decisions": "No confirmed decisions found.",
        "no_tasks": "No possible tasks found.",
        "no_risks": "No possible risks found.",
        "no_questions": "No possible questions found.",
        "no_evidence": "No supporting excerpts found.",
        "no_topic": "No source-backed evidence found for this topic.",
        "summary_found": "Found {excerpts} source-backed excerpt(s) across "
        "{meetings} meeting(s) for: {terms}.",
    }


def graph_node_title(nodes: list[dict[str, object]], node_id: int) -> str:
    for node in nodes:
        if int(str(node["id"])) == node_id:
            return str(node["title"])
    return str(node_id)


def entity_source(row: dict[str, object]) -> str:
    source_parts = [
        str(row.get("meeting_external_id") or row.get("meeting_id") or ""),
        str(row.get("chunk_external_id") or row.get("source_chunk_id") or ""),
    ]
    source = " / ".join(part for part in source_parts if part)
    if row.get("chunk_timestamp_label"):
        return f"{source} @ {row['chunk_timestamp_label']}"
    return source


def embedding_label(row: dict[str, object], provider: object) -> str:
    provider_name = str(row.get("embedding_provider") or getattr(provider, "name", ""))
    model = str(row.get("embedding_model") or getattr(provider, "model", ""))
    dimensions = row.get("embedding_dimensions") or getattr(provider, "dims", None)
    suffix = f"/{dimensions}d" if dimensions else ""
    return f"{provider_name}/{model}{suffix}"


def float_value(value: object, label: str) -> float:
    if isinstance(value, int | float):
        return float(value)
    message = f"Expected numeric {label}."
    raise RuntimeError(message)


def print_search_meeting_summaries(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No matching meetings.")
        return
    seen: set[int] = set()
    for row in rows:
        meeting_id = int(cast("int | str", row["meeting_id"]))
        if meeting_id in seen:
            continue
        seen.add(meeting_id)
        date = compact_date(row.get("updated_at") or row.get("created_at"))
        suffix = f" ({date})" if date else ""
        print_text_block(f"- #{meeting_id} {row['title']}{suffix} | open: mm open {meeting_id}")


def print_meeting_summaries(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No matching meetings.")
        return
    for row in rows:
        print_text_block(f"- #{row['id']} {row['title']} | open: mm open {row['id']}")


def print_entity_bullets(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No structured signals.")
        return
    for row in rows:
        print_text_block(f"- {row['text']} | Source: {entity_source(row)}")


def print_topic_summary(
    memory: dict[str, object],
    evidence: list[dict[str, object]],
) -> None:
    labels = topic_labels(str(memory.get("ui_language") or "en"))
    query_terms = cast("list[str]", memory.get("query_terms", []))
    meeting_ids = {row.get("meeting_id") for row in evidence}
    if not evidence:
        print_text_block(labels["no_topic"])
        return
    term_text = ", ".join(query_terms) if query_terms else str(memory["topic"])
    print_text_block(
        labels["summary_found"].format(
            excerpts=len(evidence),
            meetings=len(meeting_ids),
            terms=term_text,
        )
    )


def print_topic_section(
    memory: dict[str, object],
    evidence: list[dict[str, object]],
    kind: str,
    labels: dict[str, str],
    *,
    structured_rows: list[dict[str, object]] | None = None,
) -> None:
    rows = structured_rows if structured_rows is not None else entity_rows_for_kind(memory, kind)
    if rows:
        print_entity_bullets(rows)
        return
    if kind == "decisions":
        print_text_block(labels["no_decisions"])
        return
    print_evidence_bullets(classify_evidence(evidence, kind), empty=empty_label(labels, kind))


def empty_label(labels: dict[str, str], kind: str) -> str:
    empty_labels = {
        "action_items": "no_tasks",
        "risks": "no_risks",
        "open_questions": "no_questions",
    }
    return labels[empty_labels[kind]]


def classify_evidence(
    evidence: list[dict[str, object]],
    kind: str,
) -> list[dict[str, object]]:
    keywords = TOPIC_KEYWORDS[kind]
    return [
        row
        for row in evidence
        if any(keyword in str(row.get("text", "")).casefold() for keyword in keywords)
    ]


TOPIC_KEYWORDS = {
    "decisions": (
        "decided",
        "decision",
        "agreed",
        "approved",
        "use ",
        "using",
        "chosen",
        "selected",
        "решили",
        "договорились",
        "используем",
        "выбрали",
        "надо делать",
    ),
    "action_items": (
        "action",
        "todo",
        "send",
        "prepare",
        "owner",
        "owns",
        "by friday",
        "надо",
        "нужно",
        "сделать",
        "отправить",
    ),
    "risks": (
        "risk",
        "problem",
        "cannot",
        "can't",
        "blocked",
        "blocker",
        "race",
        "consistency",
        "inconsistent",
        "риск",
        "проблем",
        "не можем",
        "ломается",
        "рассинхрон",
        "гонка",
        "нет гарант",
    ),
    "open_questions": (
        "question",
        "unclear",
        "unknown",
        "how ",
        "what ",
        "who ",
        "вопрос",
        "непонятно",
        "как будем",
        "нужно решить",
    ),
}


def print_evidence_bullets(
    rows: list[dict[str, object]],
    *,
    empty: str = "No relevant evidence.",
) -> None:
    if not rows:
        print_text_block(empty)
        return
    for row in rows:
        prefix_parts = []
        if row.get("timestamp_label"):
            prefix_parts.append(str(row["timestamp_label"]))
        if row.get("speaker"):
            prefix_parts.append(str(row["speaker"]))
        prefix = f"{' | '.join(prefix_parts)}: " if prefix_parts else ""
        print_text_block(
            f"- {prefix}{row['text']} | Source: {entity_source(row)} "
            f"| open: mm open {row['meeting_id']}"
        )


def print_grouped_entity_bullets(rows: list[dict[str, object]]) -> None:
    if not rows:
        print_text_block("No structured signals.")
        return
    for row in rows:
        kind = str(row.get("kind", "signal")).replace("_", " ")
        print_text_block(f"- {kind}: {row['text']} | Source: {entity_source(row)}")
