from typing import Any

import orjson

JSON_OPTIONS = orjson.OPT_SORT_KEYS


def dumps_json_bytes(value: Any) -> bytes:  # noqa: ANN401
    return orjson.dumps(value, default=str, option=JSON_OPTIONS)


def dumps_json(value: Any) -> str:  # noqa: ANN401
    return dumps_json_bytes(value).decode()


def loads_json(value: bytes | bytearray | memoryview | str) -> Any:  # noqa: ANN401
    return orjson.loads(value)
