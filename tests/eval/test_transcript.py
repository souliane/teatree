from pathlib import Path

from teatree.eval.models import TokenUsage
from teatree.eval.transcript import (
    extract_billed_model,
    extract_terminal_reason,
    extract_text_blocks,
    extract_tool_calls,
    extract_usage,
    parse_stream_json,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestParseStreamJson:
    def test_parses_one_event_per_line(self) -> None:
        events = parse_stream_json(_load("worktree_first_pass.stream.jsonl"))
        assert [e.type for e in events] == ["system", "assistant", "user", "assistant", "result"]

    def test_records_subtype_when_present(self) -> None:
        events = parse_stream_json(_load("worktree_first_pass.stream.jsonl"))
        system_event = next(e for e in events if e.type == "system")
        result_event = next(e for e in events if e.type == "result")
        assert system_event.subtype == "init"
        assert result_event.subtype == "success"

    def test_skips_blank_and_non_json_lines(self) -> None:
        stream = '{"type":"system","subtype":"init"}\n\nnot-json\n{"type":"result","subtype":"success"}\n'
        events = parse_stream_json(stream)
        assert [e.type for e in events] == ["system", "result"]

    def test_line_no_is_one_indexed(self) -> None:
        events = parse_stream_json('{"type":"system"}\n{"type":"result","subtype":"success"}\n')
        assert events[0].line_no == 1
        assert events[1].line_no == 2

    def test_drops_events_without_string_type(self) -> None:
        events = parse_stream_json('{"foo": 1}\n{"type": 42}\n{"type": "result", "subtype": "success"}\n')
        assert [e.type for e in events] == ["result"]


class TestExtractToolCalls:
    def test_assistant_turns_are_one_indexed(self) -> None:
        events = parse_stream_json(_load("worktree_first_pass.stream.jsonl"))
        calls = extract_tool_calls(events)
        assert [c.turn for c in calls] == [1, 2]

    def test_captures_tool_name_and_input(self) -> None:
        events = parse_stream_json(_load("worktree_first_pass.stream.jsonl"))
        calls = extract_tool_calls(events)
        assert calls[0].name == "Bash"
        assert calls[0].input["command"].startswith("git worktree add")

    def test_ignores_text_blocks(self) -> None:
        events = parse_stream_json(_load("worktree_first_pass.stream.jsonl"))
        calls = extract_tool_calls(events)
        assert all(c.name == "Bash" for c in calls)
        assert len(calls) == 2


class TestExtractTextBlocks:
    def test_returns_text_blocks_from_assistant_events(self) -> None:
        events = parse_stream_json(_load("worktree_first_pass.stream.jsonl"))
        text_blocks = extract_text_blocks(events)
        assert text_blocks == ["I'll create a worktree first."]


class TestExtractTerminalReason:
    def test_returns_subtype_and_is_error_from_result_event(self) -> None:
        events = parse_stream_json(_load("worktree_first_pass.stream.jsonl"))
        reason, is_error = extract_terminal_reason(events)
        assert reason == "success"
        assert is_error is False

    def test_returns_aborted_when_no_result_event(self) -> None:
        events = parse_stream_json(_load("aborted.stream.jsonl"))
        reason, is_error = extract_terminal_reason(events)
        assert reason == "aborted"
        assert is_error is True

    def test_handles_error_subtype(self) -> None:
        stream = (
            '{"type": "system", "subtype": "init"}\n'
            '{"type": "result", "subtype": "error_max_turns", "is_error": true}\n'
        )
        events = parse_stream_json(stream)
        reason, is_error = extract_terminal_reason(events)
        assert reason == "error_max_turns"
        assert is_error is True


class TestExtractUsage:
    def test_full_usage_event_populates_token_usage(self) -> None:
        stream = (
            '{"type":"result","subtype":"success","usage":{"input_tokens":120,'
            '"cache_creation_input_tokens":340,"cache_read_input_tokens":6500,"output_tokens":80}}\n'
        )
        events = parse_stream_json(stream)
        assert extract_usage(events) == TokenUsage(input=120, cache_creation=340, cache_read=6500, output=80)

    def test_missing_usage_yields_all_zero(self) -> None:
        events = parse_stream_json('{"type":"result","subtype":"success"}\n')
        assert extract_usage(events) == TokenUsage()

    def test_no_result_event_yields_all_zero(self) -> None:
        events = parse_stream_json(_load("aborted.stream.jsonl"))
        assert extract_usage(events) == TokenUsage()

    def test_partial_and_non_int_keys_default_to_zero(self) -> None:
        stream = '{"type":"result","subtype":"success","usage":{"input_tokens":50,"output_tokens":"oops"}}\n'
        events = parse_stream_json(stream)
        assert extract_usage(events) == TokenUsage(input=50)

    def test_non_dict_usage_yields_all_zero(self) -> None:
        events = parse_stream_json('{"type":"result","subtype":"success","usage":"not a dict"}\n')
        assert extract_usage(events) == TokenUsage()


class TestExtractBilledModel:
    def test_returns_dominant_model_usage_key(self) -> None:
        stream = (
            '{"type":"result","subtype":"success","model_usage":'
            '{"claude-sonnet-4-6":{"input_tokens":10},"claude-opus-4-8":{"input_tokens":900}}}\n'
        )
        events = parse_stream_json(stream)
        assert extract_billed_model(events) == "claude-opus-4-8"

    def test_returns_none_when_model_usage_absent(self) -> None:
        events = parse_stream_json('{"type":"result","subtype":"success"}\n')
        assert extract_billed_model(events) is None

    def test_returns_none_when_no_result_event(self) -> None:
        events = parse_stream_json(_load("aborted.stream.jsonl"))
        assert extract_billed_model(events) is None

    def test_single_model_usage_key_is_the_billed_model(self) -> None:
        stream = '{"type":"result","subtype":"success","model_usage":{"claude-haiku-4-5":{"input_tokens":42}}}\n'
        events = parse_stream_json(stream)
        assert extract_billed_model(events) == "claude-haiku-4-5"

    def test_non_dict_model_usage_yields_none(self) -> None:
        events = parse_stream_json('{"type":"result","subtype":"success","model_usage":[1,2]}\n')
        assert extract_billed_model(events) is None


class TestMalformedStreams:
    """Defensive paths for ``claude -p`` output that doesn't match the spec."""

    def test_drops_non_dict_json_lines(self) -> None:
        # Top-level JSON arrays must be ignored — only dict events count.
        events = parse_stream_json('[1, 2, 3]\n{"type":"result","subtype":"success"}\n')
        assert [e.type for e in events] == ["result"]

    def test_ignores_assistant_event_without_message_dict(self) -> None:
        stream = '{"type":"assistant"}\n{"type":"assistant","message":"not a dict"}\n'
        events = parse_stream_json(stream)
        assert extract_tool_calls(events) == []
        assert extract_text_blocks(events) == []

    def test_ignores_assistant_event_with_non_list_content(self) -> None:
        stream = '{"type":"assistant","message":{"content":"not a list"}}\n'
        events = parse_stream_json(stream)
        assert extract_tool_calls(events) == []
        assert extract_text_blocks(events) == []

    def test_ignores_non_dict_content_items(self) -> None:
        stream = '{"type":"assistant","message":{"content":["string","not dict",42]}}\n'
        events = parse_stream_json(stream)
        assert extract_tool_calls(events) == []
        assert extract_text_blocks(events) == []

    def test_ignores_tool_use_without_string_name(self) -> None:
        stream = '{"type":"assistant","message":{"content":[{"type":"tool_use","name":42,"input":{}}]}}\n'
        events = parse_stream_json(stream)
        assert extract_tool_calls(events) == []

    def test_ignores_text_block_with_non_string_text(self) -> None:
        stream = '{"type":"assistant","message":{"content":[{"type":"text","text":42}]}}\n'
        events = parse_stream_json(stream)
        assert extract_text_blocks(events) == []

    def test_tool_use_with_non_dict_input_falls_back_to_empty_dict(self) -> None:
        stream = '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":"raw"}]}}\n'
        events = parse_stream_json(stream)
        calls = extract_tool_calls(events)
        assert calls[0].name == "Bash"
        assert calls[0].input == {}
