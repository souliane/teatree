import math

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from teatree.agents.lane_b import compaction as compaction_mod
from teatree.agents.lane_b.compaction import (
    DEFAULT_KEEP_RECENT,
    CompactionPolicy,
    compact_history,
    elide_stale_tool_results,
)


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


def _big_return(call_id: str, size: int = 50_000) -> ModelRequest:
    return ModelRequest(parts=[ToolReturnPart(tool_name="Bash", content="x" * size, tool_call_id=call_id)])


def _tool_trajectory(turns: int, *, size: int = 50_000) -> list[ModelMessage]:
    """A ``turns``-turn call→return trajectory, short enough that the message trim never engages."""
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="task")])]
    for i in range(turns):
        history.extend((_tool_call(f"c{i}"), _big_return(f"c{i}", size)))
    return history


def _returned(msgs: list[ModelMessage]) -> list[ToolReturnPart]:
    return [p for m in msgs if isinstance(m, ModelRequest) for p in m.parts if isinstance(p, ToolReturnPart)]


def _chars(msgs: list[ModelMessage]) -> int:
    return sum(len(p.model_response_str()) for p in _returned(msgs))


class TestStaleToolResultsAreElided:
    """A ≤20-turn run is never message-trimmed, yet re-sends every tool result (#4816)."""

    def test_a_short_trajectory_the_trim_never_touches_is_still_shrunk(self) -> None:
        history = _tool_trajectory(20)
        assert len(history) <= DEFAULT_KEEP_RECENT + 1
        assert _chars(elide_stale_tool_results(history)) <= _chars(history) * 0.3

    def test_the_most_recent_results_stay_verbatim(self) -> None:
        history = _tool_trajectory(20)
        out = elide_stale_tool_results(history, 6)
        assert [p.content for p in _returned(out[-6:])] == [p.content for p in _returned(history[-6:])]

    def test_the_pinned_head_is_never_touched(self) -> None:
        history = _tool_trajectory(20)
        history[0] = _big_return("head-call")
        assert elide_stale_tool_results(history)[0] is history[0]

    def test_the_stub_names_the_size_and_the_tool(self) -> None:
        stub = str(_returned(elide_stale_tool_results(_tool_trajectory(20)))[0].content)
        assert "50000 chars" in stub
        assert "`Bash`" in stub

    def test_a_bash_stub_does_not_invite_a_blind_rerun(self) -> None:
        stub = str(_returned(elide_stale_tool_results(_tool_trajectory(20)))[0].content)
        assert "re-run only if the command is read-only" in stub

    @pytest.mark.parametrize("tool_name", ["Read", "Grep"])
    def test_a_read_stub_invites_a_reread(self, tool_name: str) -> None:
        history = _tool_trajectory(20)
        history[2] = ModelRequest(parts=[ToolReturnPart(tool_name=tool_name, content="x" * 50_000, tool_call_id="c0")])
        stub = str(_returned(elide_stale_tool_results(history))[0].content)
        assert "re-read to obtain them" in stub
        assert "re-run" not in stub

    def test_restubbing_preserves_the_original_size(self) -> None:
        # pydantic_ai writes the processed history back into the run, so a stub is processed again.
        first = elide_stale_tool_results(_tool_trajectory(20, size=175_185))
        assert "175185 chars" in str(_returned(first)[0].content)
        second = elide_stale_tool_results(first)
        assert "175185 chars" in str(_returned(second)[0].content)

    def test_the_call_return_pairing_survives(self) -> None:
        out = elide_stale_tool_results(_tool_trajectory(20))
        assert _orphaned_return_ids(out) == []
        assert [p.tool_call_id for p in _returned(out)] == [f"c{i}" for i in range(20)]

    def test_a_result_smaller_than_its_stub_is_left_alone(self) -> None:
        history = _tool_trajectory(20, size=3)
        assert elide_stale_tool_results(history) == history

    def test_zero_disables_the_pass(self) -> None:
        history = _tool_trajectory(20)
        assert elide_stale_tool_results(history, 0) == history

    def test_the_caller_history_is_not_mutated(self) -> None:
        history = _tool_trajectory(20)
        before = _chars(history)
        elide_stale_tool_results(history)
        assert _chars(history) == before

    def test_compact_history_no_longer_stubs(self) -> None:
        history = _tool_trajectory(20)
        assert compact_history(history, policy=CompactionPolicy()) == history

    def test_the_per_phase_override_is_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            compaction_mod.cold_reader, "read_setting", lambda key: {"coding": 2} if "tool" in key else None
        )
        assert CompactionPolicy.for_phase("coding").keep_tool_results == 2


class TestZeroKeepToolResultsDisablesTheStubPass:
    def test_a_stored_zero_resolves_to_zero_and_stubs_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            compaction_mod.cold_reader, "read_setting", lambda key: {"coding": 0} if "tool" in key else None
        )
        keep = CompactionPolicy.for_phase("coding").keep_tool_results
        assert keep == 0
        history = _tool_trajectory(20)
        assert elide_stale_tool_results(history, keep) == history

    @pytest.mark.parametrize("value", [-1, True, "6", None])
    def test_an_invalid_override_gives_the_default(self, value: object, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            compaction_mod.cold_reader, "read_setting", lambda key: {"coding": value} if "tool" in key else None
        )
        assert CompactionPolicy.for_phase("coding").keep_tool_results == compaction_mod.DEFAULT_KEEP_TOOL_RESULTS


def _stubbed_indices(msgs: list[ModelMessage]) -> set[int]:
    return {
        index
        for index, message in enumerate(msgs)
        if isinstance(message, ModelRequest)
        and any(isinstance(p, ToolReturnPart) and str(p.content).startswith("[elided: ") for p in message.parts)
    }


def _first_difference(prev: list[bytes], nxt: list[bytes]) -> int:
    return next((i for i, (a, b) in enumerate(zip(prev, nxt, strict=False)) if a != b), len(prev))


class TestTheStubBoundaryIsCacheStable:
    """Consecutive requests must share a byte-identical prefix so the provider cache keeps hitting."""

    def test_consecutive_requests_share_a_byte_identical_prefix_between_steps(self) -> None:
        keep, requests = 6, 30
        history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="task")])]
        sent: list[list[bytes]] = []
        cutoffs: list[int] = []
        for i in range(requests):
            history = elide_stale_tool_results([*history, _tool_call(f"c{i}"), _big_return(f"c{i}")], keep)
            sent.append([ModelMessagesTypeAdapter.dump_json([m]) for m in history])
            cutoffs.append(max(_stubbed_indices(history), default=0) + 1)
        changes = 0
        for n in range(1, requests):
            prev, nxt = sent[n - 1], sent[n]
            if prev == nxt[: len(prev)]:
                continue
            changes += 1
            assert _first_difference(prev, nxt) >= cutoffs[n - 1], f"request {n} rewrote bytes before the old boundary"
        assert changes <= math.ceil(requests / 3)

    @pytest.mark.parametrize(("start", "stop"), [(13, 19), (19, 25), (25, 31)])
    def test_the_stub_boundary_only_moves_in_steps_of_keep_tool_results(self, start: int, stop: int) -> None:
        trajectory = _tool_trajectory(20)
        stubbed = {frozenset(_stubbed_indices(elide_stale_tool_results(trajectory[:n], 6))) for n in range(start, stop)}
        assert len(stubbed) == 1

    def test_the_verbatim_tail_never_exceeds_two_steps(self) -> None:
        trajectory = _tool_trajectory(20)
        for n in range(1, len(trajectory) + 1):
            out = elide_stale_tool_results(trajectory[:n], 6)
            assert n - (max(_stubbed_indices(out), default=0) + 1) <= 11
