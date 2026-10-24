import asyncio
import math
from types import SimpleNamespace

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
from pydantic_ai.usage import RunUsage

from teatree.agents.lane_b import compaction as compaction_mod
from teatree.agents.lane_b.compaction import CompactionPolicy, elide_stale_tool_results
from teatree.agents.lane_b.tool_names import TOOL_READ


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


class TestLateCompaction:
    def test_large_final_message_stays_whole_after_older_history_is_summarized(self) -> None:
        async def summarize(_text: str, _usage: RunUsage) -> str:
            await asyncio.sleep(0)
            return "older work"

        final = ModelRequest(parts=[UserPromptPart(content="x" * 100_000)])
        history = [ModelRequest(parts=[UserPromptPart(content="task")]), *_msgs(4), final]
        ctx = SimpleNamespace(context_window_used=0.95, model=SimpleNamespace(context_window=200_000), usage=RunUsage())

        result = asyncio.run(compaction_mod.LateHistoryCompactor(summarize)(ctx, history))

        assert result[-1] is final
        assert "Earlier conversation summary:" in repr(result[1])

    def test_target_reduction_keeps_every_tool_return_with_its_call(self) -> None:
        async def summarize(_text: str, _usage: RunUsage) -> str:
            await asyncio.sleep(0)
            return "older work"

        history = [ModelRequest(parts=[UserPromptPart(content="task" * 10_000)])]
        for index in range(10):
            history.extend((_tool_call(f"c{index}"), _big_return(f"c{index}", 8_000)))
        ctx = SimpleNamespace(context_window_used=0.95, model=SimpleNamespace(context_window=32_000), usage=RunUsage())

        result = asyncio.run(compaction_mod.LateHistoryCompactor(summarize, keep_tool_results=0)(ctx, history))

        assert _orphaned_return_ids(result) == []
        assert "Earlier conversation summary:" in repr(result[1])

    def test_native_and_one_million_suffix_window_fallbacks(self) -> None:
        assert compaction_mod.context_window_for_model("anthropic/claude-opus-4-7", None) == 1_000_000
        assert compaction_mod.context_window_for_model("router/unknown[1m]", None) == 1_000_000

    def test_operator_steer_in_stale_history_survives_verbatim(self) -> None:
        async def summarize(_text: str, _usage: RunUsage) -> str:
            await asyncio.sleep(0)
            return "Previous work summarized"

        steer = "[TeaTree operator input id=steer-1] Preserve the public API. [end operator input]"
        history = [
            ModelRequest(parts=[UserPromptPart(content="task")]),
            ModelRequest(parts=[UserPromptPart(content=steer)]),
            ModelResponse(parts=[TextPart(content="old " * 1000)]),
            ModelRequest(parts=[UserPromptPart(content="continue")]),
        ]
        ctx = SimpleNamespace(context_window_used=None, model=SimpleNamespace(context_window=1000), usage=RunUsage())

        result = asyncio.run(compaction_mod.LateHistoryCompactor(summarize, reserve_tokens=160)(ctx, history))

        assert steer in repr(result)

    def test_operator_followup_in_stale_history_survives_verbatim(self) -> None:
        async def summarize(_text: str, _usage: RunUsage) -> str:
            await asyncio.sleep(0)
            return "Previous work summarized"

        followup = "[TeaTree operator follow-up id=idle-1] Keep the API stable. [end operator follow-up]"
        history = [
            ModelRequest(parts=[UserPromptPart(content="task")]),
            ModelRequest(parts=[UserPromptPart(content=followup)]),
            ModelResponse(parts=[TextPart(content="old " * 1000)]),
            ModelRequest(parts=[UserPromptPart(content="continue")]),
        ]
        ctx = SimpleNamespace(context_window_used=None, model=SimpleNamespace(context_window=1000), usage=RunUsage())

        result = asyncio.run(compaction_mod.LateHistoryCompactor(summarize, reserve_tokens=160)(ctx, history))

        assert followup in repr(result)

    def test_superseded_read_is_stubbed_before_summary(self) -> None:
        calls = [
            ModelResponse(parts=[ToolCallPart(tool_name=TOOL_READ, args={"file_path": "/a.py"}, tool_call_id=key)])
            for key in ("old", "new")
        ]
        history = [
            ModelRequest(parts=[UserPromptPart(content="task")]),
            calls[0],
            ModelRequest(parts=[ToolReturnPart(tool_name=TOOL_READ, content="old" * 6000, tool_call_id="old")]),
            calls[1],
            ModelRequest(parts=[ToolReturnPart(tool_name=TOOL_READ, content="latest", tool_call_id="new")]),
        ]
        summaries: list[str] = []

        async def summarize(text: str, _usage: RunUsage) -> str:
            await asyncio.sleep(0)
            summaries.append(text)
            return "summary"

        ctx = SimpleNamespace(context_window_used=None, model=SimpleNamespace(context_window=2000), usage=RunUsage())
        result = asyncio.run(compaction_mod.LateHistoryCompactor(summarize, reserve_tokens=160)(ctx, history))

        assert "old" * 100 not in repr(result)
        assert "latest" in repr(result)
        assert summaries == []

    def test_forced_compaction_with_unknown_window_still_summarizes(self) -> None:
        summaries: list[str] = []

        async def summarize(text: str, _usage: RunUsage) -> str:
            await asyncio.sleep(0)
            summaries.append(text)
            return "Older work"

        history = [ModelRequest(parts=[UserPromptPart(content="task")]), *_msgs(30)]
        ctx = SimpleNamespace(context_window_used=None, model=SimpleNamespace(context_window=None), usage=RunUsage())
        compactor = compaction_mod.LateHistoryCompactor(summarize)
        compactor.force_next()

        result = asyncio.run(compactor(ctx, history))

        assert len(summaries) == 1
        assert len(result) < len(history)

    def test_compaction_lands_below_half_the_trigger(self) -> None:
        async def summarize(_text: str, _usage: RunUsage) -> str:
            await asyncio.sleep(0)
            return "Earlier work " * 90

        history = [
            ModelRequest(parts=[UserPromptPart(content="task")]),
            ModelResponse(parts=[TextPart(content="old " * 1000)]),
            ModelResponse(parts=[TextPart(content="recent " * 70)]),
            ModelRequest(parts=[UserPromptPart(content="continue")]),
        ]
        ctx = SimpleNamespace(context_window_used=None, model=SimpleNamespace(context_window=1000), usage=RunUsage())

        result = asyncio.run(compaction_mod.LateHistoryCompactor(summarize, reserve_tokens=160)(ctx, history))

        assert compaction_mod._estimated_tokens(result) <= 420
        assert "continue" in repr(result)

    def test_explicit_model_window_overrides_a_wrong_registry_window(self) -> None:
        summaries: list[str] = []

        async def summarize(text: str, _usage: RunUsage) -> str:
            await asyncio.sleep(0)
            summaries.append(text)
            return "older work"

        history = [
            ModelRequest(parts=[UserPromptPart(content="task")]),
            ModelResponse(parts=[TextPart(content="old " * 700)]),
            ModelRequest(parts=[UserPromptPart(content="recent direction")]),
        ]
        ctx = SimpleNamespace(
            context_window_used=0.09,
            model=SimpleNamespace(model_name="router/x", context_window=10_000),
            usage=RunUsage(),
        )
        compactor = compaction_mod.LateHistoryCompactor(
            summarize, reserve_tokens=160, keep_recent_tokens=200, context_windows={"router/x": 1_000}
        )

        result = asyncio.run(compactor(ctx, history))

        assert summaries
        assert "recent direction" in repr(result)

    def test_history_stays_intact_below_the_token_reserve_threshold(self) -> None:
        summaries: list[str] = []

        async def summarize(text: str, _usage: RunUsage) -> str:
            await asyncio.sleep(0)
            summaries.append(text)
            return "earlier decisions"

        history = [ModelRequest(parts=[UserPromptPart(content="task")]), *_msgs(80)]
        ctx = SimpleNamespace(context_window_used=0.83, model=SimpleNamespace(context_window=1000), usage=RunUsage())
        compactor = compaction_mod.LateHistoryCompactor(summarize, reserve_tokens=160, keep_recent_tokens=200)

        result = asyncio.run(compactor(ctx, history))

        assert result == history
        assert summaries == []

    def test_near_window_summary_preserves_recent_inputs_and_tool_pairs(self) -> None:
        summaries: list[str] = []

        async def summarize(text: str, _usage: RunUsage) -> str:
            await asyncio.sleep(0)
            summaries.append(text)
            return "Earlier decision: keep the API"

        history: list[ModelMessage] = [
            ModelRequest(parts=[UserPromptPart(content="task")]),
            ModelResponse(parts=[TextPart(content="old decision " * 100)]),
            ModelRequest(parts=[UserPromptPart(content="old correction")]),
            ModelResponse(parts=[TextPart(content="old answer " * 100)]),
            ModelRequest(parts=[UserPromptPart(content="recent operator steer")]),
            _tool_call("c1"),
            _tool_return("c1"),
        ]
        ctx = SimpleNamespace(context_window_used=0.9, model=SimpleNamespace(context_window=1000), usage=RunUsage())
        compactor = compaction_mod.LateHistoryCompactor(summarize, reserve_tokens=160, keep_recent_tokens=200)

        result = asyncio.run(compactor(ctx, history))

        assert len(summaries) == 1
        assert "old correction" in summaries[0]
        assert "Earlier decision: keep the API" in repr(result)
        assert "recent operator steer" in repr(result)
        assert _orphaned_return_ids(result) == []


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
