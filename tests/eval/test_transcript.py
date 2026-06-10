from pathlib import Path

import pytest

from teatree.eval.models import TokenUsage
from teatree.eval.transcript import (
    extract_billed_model,
    extract_model_cost_split,
    extract_terminal_reason,
    extract_text_blocks,
    extract_tool_calls,
    extract_usage,
    parse_stream_json,
    requested_model_present,
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


class TestRequestedModelPresent:
    """``fell_back`` is the REQUESTED main model being ABSENT from ``model_usage`` keys.

    Claude Code always runs ``claude-haiku-4-5`` as a cheap auxiliary model
    alongside the requested main model, so an auxiliary key sitting beside the
    requested model is NORMAL — not a fallback. Fallback is the requested model
    being substituted away entirely.
    """

    def test_requested_present_alongside_haiku_aux_is_not_fallback(self) -> None:
        stream = (
            '{"type":"result","subtype":"success","model_usage":'
            '{"claude-haiku-4-5-20251001":{"input_tokens":9000},"claude-opus-4-8":{"input_tokens":80}}}\n'
        )
        events = parse_stream_json(stream)
        assert requested_model_present(events, "claude-opus-4-8") is True

    def test_requested_substituted_by_sonnet_is_fallback(self) -> None:
        stream = '{"type":"result","subtype":"success","model_usage":{"claude-sonnet-4-6":{"input_tokens":900}}}\n'
        events = parse_stream_json(stream)
        assert requested_model_present(events, "claude-opus-4-8") is False

    def test_requested_absent_with_only_haiku_and_sonnet_is_fallback(self) -> None:
        stream = (
            '{"type":"result","subtype":"success","model_usage":'
            '{"claude-haiku-4-5":{"input_tokens":9000},"claude-sonnet-4-6":{"input_tokens":900}}}\n'
        )
        events = parse_stream_json(stream)
        assert requested_model_present(events, "claude-opus-4-8") is False

    def test_dated_model_usage_key_matches_undated_request(self) -> None:
        stream = (
            '{"type":"result","subtype":"success","model_usage":{"claude-opus-4-8-20251001":{"input_tokens":80}}}\n'
        )
        events = parse_stream_json(stream)
        assert requested_model_present(events, "claude-opus-4-8") is True

    def test_effort_variant_request_compares_on_base_model(self) -> None:
        stream = '{"type":"result","subtype":"success","model_usage":{"claude-opus-4-8":{"input_tokens":80}}}\n'
        events = parse_stream_json(stream)
        assert requested_model_present(events, "claude-opus-4-8@xhigh") is True

    def test_unobservable_model_usage_is_none_not_a_fallback(self) -> None:
        events = parse_stream_json('{"type":"result","subtype":"success"}\n')
        assert requested_model_present(events, "claude-opus-4-8") is None

    def test_no_result_event_is_none(self) -> None:
        events = parse_stream_json(_load("aborted.stream.jsonl"))
        assert requested_model_present(events, "claude-opus-4-8") is None


class TestExtractModelCostSplit:
    """Split metered cost into the requested MAIN model vs the AUXILIARY background.

    Each ``model_usage`` entry carries a per-model ``costUSD`` (the CLI's
    camelCase key). The split keys the requested base model's cost as ``main``
    and sums everything else as ``aux``.
    """

    def test_splits_main_from_haiku_aux(self) -> None:
        stream = (
            '{"type":"result","subtype":"success","model_usage":'
            '{"claude-haiku-4-5-20251001":{"costUSD":0.02,"inputTokens":9000,"outputTokens":40},'
            '"claude-opus-4-8":{"costUSD":0.5,"inputTokens":80,"outputTokens":200}}}\n'
        )
        events = parse_stream_json(stream)
        split = extract_model_cost_split(events, "claude-opus-4-8")
        assert split.main_cost_usd == pytest.approx(0.5)
        assert split.aux_cost_usd == pytest.approx(0.02)

    def test_main_zero_when_requested_model_absent(self) -> None:
        stream = (
            '{"type":"result","subtype":"success","model_usage":'
            '{"claude-haiku-4-5":{"costUSD":0.02},"claude-sonnet-4-6":{"costUSD":0.3}}}\n'
        )
        events = parse_stream_json(stream)
        split = extract_model_cost_split(events, "claude-opus-4-8")
        assert split.main_cost_usd == pytest.approx(0.0)
        assert split.aux_cost_usd == pytest.approx(0.32)

    def test_dated_main_key_matches_undated_request(self) -> None:
        stream = '{"type":"result","subtype":"success","model_usage":{"claude-opus-4-8-20251001":{"costUSD":0.5}}}\n'
        events = parse_stream_json(stream)
        split = extract_model_cost_split(events, "claude-opus-4-8@xhigh")
        assert split.main_cost_usd == pytest.approx(0.5)
        assert split.aux_cost_usd == pytest.approx(0.0)

    def test_main_aux_token_split_captured(self) -> None:
        stream = (
            '{"type":"result","subtype":"success","model_usage":'
            '{"claude-haiku-4-5":{"costUSD":0.02,"inputTokens":9000,"outputTokens":40,'
            '"cacheReadInputTokens":100,"cacheCreationInputTokens":5},'
            '"claude-opus-4-8":{"costUSD":0.5,"inputTokens":80,"outputTokens":200,'
            '"cacheReadInputTokens":7000,"cacheCreationInputTokens":50}}}\n'
        )
        events = parse_stream_json(stream)
        split = extract_model_cost_split(events, "claude-opus-4-8")
        assert split.main_usage == TokenUsage(input=80, output=200, cache_read=7000, cache_creation=50)
        assert split.aux_usage == TokenUsage(input=9000, output=40, cache_read=100, cache_creation=5)

    def test_no_model_usage_yields_zero_split(self) -> None:
        events = parse_stream_json('{"type":"result","subtype":"success"}\n')
        split = extract_model_cost_split(events, "claude-opus-4-8")
        assert split.main_cost_usd == pytest.approx(0.0)
        assert split.aux_cost_usd == pytest.approx(0.0)
        assert split.main_usage == TokenUsage()
        assert split.aux_usage == TokenUsage()

    def test_non_dict_model_usage_yields_zero_split(self) -> None:
        events = parse_stream_json('{"type":"result","subtype":"success","model_usage":[1,2]}\n')
        split = extract_model_cost_split(events, "claude-opus-4-8")
        assert split.main_cost_usd == pytest.approx(0.0)
        assert split.aux_cost_usd == pytest.approx(0.0)


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
