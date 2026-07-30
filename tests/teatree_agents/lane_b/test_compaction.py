import pytest
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

from teatree.agents.lane_b import compaction as compaction_mod
from teatree.agents.lane_b.compaction import DEFAULT_KEEP_RECENT, CompactionPolicy, compact_history


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


class TestCompactionPolicy:
    """The context-compaction policy object replacing the hardcoded trim (#3157 E2c)."""

    def test_default_policy_is_byte_identical_to_the_bare_keep_recent(self) -> None:
        history = _msgs(DEFAULT_KEEP_RECENT + 10)
        via_policy = compact_history(history, policy=CompactionPolicy())
        via_default = compact_history(history)
        assert via_policy == via_default

    def test_policy_keep_recent_supersedes_the_bare_argument(self) -> None:
        history = _msgs(20)
        compacted = compact_history(history, keep_recent=15, policy=CompactionPolicy(keep_recent=4))
        # keep_recent=4 wins → head + last 4 = 5 messages (no orphan trims here).
        assert len(compacted) == 5

    def test_pin_head_false_drops_the_first_message(self) -> None:
        history = _msgs(20)
        compacted = compact_history(history, policy=CompactionPolicy(keep_recent=4, pin_head=False))
        assert history[0] not in compacted
        assert len(compacted) == 4

    def test_for_phase_reads_the_db_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            compaction_mod.cold_reader,
            "read_setting",
            lambda key: {"coding": 12} if key == "agent_compaction_keep_recent" else None,
        )
        assert CompactionPolicy.for_phase("coding").keep_recent == 12
        # A phase with no override, and an absent phase, both fall back to the default.
        assert CompactionPolicy.for_phase("reviewing").keep_recent == DEFAULT_KEEP_RECENT
        assert CompactionPolicy.for_phase(None).keep_recent == DEFAULT_KEEP_RECENT

    def test_for_phase_ignores_a_non_integer_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            compaction_mod.cold_reader,
            "read_setting",
            lambda key: {"coding": "lots", "testing": 0, "shipping": True},
        )
        for phase in ("coding", "testing", "shipping"):
            assert CompactionPolicy.for_phase(phase).keep_recent == DEFAULT_KEEP_RECENT
