from meetily_memory.meeting_structure import extract_structured_entities


def test_extract_structured_entities_filters_noisy_tasks_and_questions() -> None:
    chunks = [
        {
            "id": 1,
            "text": (
                "Dobrynya agreed to send migration risks by Friday. "
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
        "Dobrynya agreed to send migration risks by Friday.",
        "Нужно подготовить список рисков к пятнице.",
    ]
    assert open_questions == [
        "Open question: who owns partner review?",
        "Уточнить, кто отвечает за интеграцию.",
    ]


def test_action_items_keep_explicit_work_and_drop_generic_task_talk() -> None:
    chunks = [
        {
            "id": 1,
            "text": (
                "Мне нужно время, чтобы изучить проекты и оценить текущий техдолг. "
                "И второй момент — доработать сам кэш для TTL. "
                "Что можно сделать? "
                "Дальше по задачам мы обсудили процесс. "
                "Эта задача двигается по спринтам."
            ),
        }
    ]

    entities = extract_structured_entities(chunks)
    action_items = [entity.text for entity in entities if entity.kind == "action_items"]

    assert action_items == [
        "Мне нужно время, чтобы изучить проекты и оценить текущий техдолг.",
        "И второй момент — доработать сам кэш для TTL.",
    ]
