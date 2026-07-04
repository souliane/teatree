from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from teatree.agents.lane_b.compaction import compact_history


def _msgs(n: int) -> list:
    out: list = []
    for i in range(n):
        if i % 2 == 0:
            out.append(ModelRequest(parts=[UserPromptPart(content=f"u{i}")]))
        else:
            out.append(ModelResponse(parts=[TextPart(content=f"a{i}")]))
    return out


def _tool_call(call_id: str) -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart(tool_name="shell", args={"command": "ls"}, tool_call_id=call_id)])


def _tool_return(call_id: str) -> ModelRequest:
    return ModelRequest(parts=[ToolReturnPart(tool_name="shell", content="out", tool_call_id=call_id)])


def _tool_retry(call_id: str) -> ModelRequest:
    return ModelRequest(parts=[RetryPromptPart(content="denied", tool_name="shell", tool_call_id=call_id)])


def _orphaned_return_ids(msgs: list[ModelMessage]) -> list[str]:
    """The ``tool_call_id``s of tool-results with no preceding matching tool-call."""
    seen_calls: set[str] = set()
    orphans: list[str] = []
    for message in msgs:
        if isinstance(message, ModelResponse):
            seen_calls.update(p.tool_call_id for p in message.parts if isinstance(p, ToolCallPart))
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                tool_linked_retry = isinstance(part, RetryPromptPart) and part.tool_name is not None
                if (isinstance(part, ToolReturnPart) or tool_linked_retry) and part.tool_call_id not in seen_calls:
                    orphans.append(part.tool_call_id)
    return orphans


class TestCompactHistory:
    def test_short_history_is_returned_unchanged(self) -> None:
        history = _msgs(5)
        assert compact_history(history, keep_recent=40) == history

    def test_long_history_keeps_first_plus_recent(self) -> None:
        history = _msgs(100)
        compacted = compact_history(history, keep_recent=10)
        assert len(compacted) == 11
        assert compacted[0] is history[0]
        assert compacted[1:] == history[-10:]

    def test_boundary_is_not_trimmed(self) -> None:
        history = _msgs(11)
        assert compact_history(history, keep_recent=10) == history

    def test_over_boundary_is_trimmed(self) -> None:
        history = _msgs(12)
        assert len(compact_history(history, keep_recent=10)) == 11

    def test_keep_recent_zero_is_a_noop(self) -> None:
        history = _msgs(50)
        assert compact_history(history, keep_recent=0) == history


class TestToolPairingPreserved:
    def test_naive_boundary_straddling_a_call_return_pair_drops_the_orphan(self) -> None:
        # Cut lands so the first kept message is the tool RETURN while its CALL
        # (one message earlier) falls in the dropped middle — a naive
        # ``[first, *last-N]`` keeps an orphaned return.
        history: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart(content="task")]),  # 0: framing (kept)
            *_msgs(3),  # 1..3: stale middle (dropped)
            _tool_call("c1"),  # 4: CALL — the last dropped message
            _tool_return("c1"),  # 5: RETURN — the first message of the kept window
            *_msgs(4),  # 6..9: fresh tail
        ]
        # keep_recent = len - 5 puts the cut exactly on the tool RETURN (index 5).
        compacted = compact_history(history, keep_recent=len(history) - 5)

        assert _orphaned_return_ids([history[0], *history[5:]]) == ["c1"], "the naive cut must orphan c1"
        assert _orphaned_return_ids(compacted) == [], "the fix must drop the orphaned leading return"
        # The framing head is kept; the orphaned return is gone.
        assert compacted[0] is history[0]
        assert history[5] not in compacted

    def test_a_call_return_pair_fully_inside_the_window_is_preserved(self) -> None:
        history: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart(content="task")]),  # 0: framing
            *_msgs(4),  # 1..4: stale middle
            _tool_call("c9"),  # 5: CALL (kept — inside the window)
            _tool_return("c9"),  # 6: RETURN (kept)
            *_msgs(2),  # 7..8: fresh tail
        ]
        compacted = compact_history(history, keep_recent=4)  # window = last 4: indices 5..8

        assert _orphaned_return_ids(compacted) == []
        assert history[5] in compacted  # the paired call survives intact
        assert history[6] in compacted

    def test_a_tool_linked_retry_return_is_also_snapped(self) -> None:
        # A gate-refusal RetryPromptPart (tool_name set) serializes as a tool
        # message too, so an orphaned leading retry must be dropped like a return.
        history: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart(content="task")]),
            *_msgs(3),
            _tool_call("c2"),  # CALL dropped with the middle
            _tool_retry("c2"),  # orphaned leading tool-retry
            *_msgs(4),
        ]
        compacted = compact_history(history, keep_recent=len(history) - 5)

        assert _orphaned_return_ids(compacted) == []
