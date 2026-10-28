from teatree.utils.machine_json import last_json_object


def test_last_json_object_ignores_prose_and_malformed_tail() -> None:
    text = 'note\n{"action": "post"}\n{not-json}\n'
    assert last_json_object(text) == {"action": "post"}
    assert last_json_object("note\nmore note") is None
