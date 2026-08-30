import shlex


def markdown_inline_code(value: str) -> str:
    backtick_count = value.count("`")
    delimiter = "`" * (backtick_count + 1)
    padding = " " if backtick_count else ""
    return f"{delimiter}{padding}{value}{padding}{delimiter}"


def stable_meeting_open_command(source_uuid: object, external_id: object) -> str:
    return shlex.join(
        [
            "mm",
            "open",
            "--source-uuid",
            str(source_uuid),
            "--external-id",
            str(external_id),
        ]
    )
