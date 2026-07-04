from meetily_memory.meeting_structure import extract_structured_entities


def test_extract_structured_entities_filters_noisy_tasks_and_questions() -> None:
    chunks = [
        {
            "id": 1,
            "text": (
                "Vladimir agreed to send migration risks by Friday. "
                "Нужно подготовить список рисков к пятнице. "
                "Надо подумать, что делать дальше. "
                "И этот человек должен будет писать код. "
                "Open question: who owns partner review? "
                "Уточнить, кто отвечает за интеграцию. "
                "Как вы обсуждали эту тему? "
                "Что это значит?"
            ),
        }
    ]

    entities = extract_structured_entities(chunks)
    action_items = [entity.text for entity in entities if entity.kind == "action_items"]
    open_questions = [entity.text for entity in entities if entity.kind == "open_questions"]

    assert action_items == [
        "Vladimir agreed to send migration risks by Friday.",
        "Нужно подготовить список рисков к пятнице.",
    ]
    assert open_questions == [
        "Open question: who owns partner review?",
        "Уточнить, кто отвечает за интеграцию.",
    ]
