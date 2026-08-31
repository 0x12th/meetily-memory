import shlex

from meetily_memory.domain import MeetingRef


def markdown_inline_code(value: str) -> str:
    backtick_count = value.count("`")
    delimiter = "`" * (backtick_count + 1)
    padding = " " if backtick_count else ""
    return f"{delimiter}{padding}{value}{padding}{delimiter}"


def stable_meeting_open_command(meeting_ref: MeetingRef) -> str:
    return shlex.join(["mm", "open", str(meeting_ref)])
