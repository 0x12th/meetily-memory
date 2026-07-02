from meetily_memory.json_codec import dumps_json, dumps_json_bytes, loads_json


def test_json_codec_uses_stable_sorted_unicode_output() -> None:
    payload = {"z": "край", "a": 1}

    assert dumps_json(payload) == '{"a":1,"z":"край"}'
    assert dumps_json_bytes(payload) == b'{"a":1,"z":"\xd0\xba\xd1\x80\xd0\xb0\xd0\xb9"}'
    assert loads_json(dumps_json(payload)) == payload
