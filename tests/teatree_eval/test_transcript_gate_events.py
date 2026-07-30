"""`extract_gate_events` reads the runner-synthesized hook_response events."""

from teatree.eval.transcript import _GATE_OUTPUT_SNIPPET_CAP, extract_gate_events, parse_stream_json


def _events(*lines: str) -> list:
    return parse_stream_json("\n".join(lines))


def test_hook_response_event_becomes_a_gate_event() -> None:
    events = _events(
        '{"type":"system","subtype":"hook_response","hook_event":"Stop",'
        '"outcome":"block","output":"decision: block — re-ask via AskUserQuestion"}'
    )
    gate_events = extract_gate_events(events)
    assert len(gate_events) == 1
    assert gate_events[0].hook_event_name == "Stop"
    assert gate_events[0].is_stop_block is True


def test_a_non_block_pretooluse_response_is_not_a_stop_block() -> None:
    events = _events('{"type":"system","subtype":"hook_response","hook_event":"PreToolUse","outcome":"allow"}')
    gate_events = extract_gate_events(events)
    assert len(gate_events) == 1
    assert gate_events[0].is_stop_block is False


def test_hook_started_and_assistant_events_are_ignored() -> None:
    events = _events(
        '{"type":"system","subtype":"hook_started","hook_event":"Stop"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"hi"}]}}',
        '{"type":"result","subtype":"success","is_error":false}',
    )
    assert extract_gate_events(events) == []


def test_dict_outcome_is_flattened_and_snippet_is_capped() -> None:
    long_output = "x" * (_GATE_OUTPUT_SNIPPET_CAP + 50)
    events = parse_stream_json(
        '{"type":"system","subtype":"hook_response","hook_event":"Stop",'
        '"outcome":{"decision":"block"},"output":"' + long_output + '"}'
    )
    gate_events = extract_gate_events(events)
    assert '"decision": "block"' in gate_events[0].outcome
    assert gate_events[0].is_stop_block is True
    assert len(gate_events[0].output_snippet) == _GATE_OUTPUT_SNIPPET_CAP


def test_replay_transcript_with_no_hook_events_yields_empty() -> None:
    events = _events(
        '{"type":"system","subtype":"init","session_id":"s"}',
        '{"type":"assistant","message":{"content":[{"type":"text","text":"done"}]}}',
        '{"type":"result","subtype":"success","is_error":false}',
    )
    assert extract_gate_events(events) == []
