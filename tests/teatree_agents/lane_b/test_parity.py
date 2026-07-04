"""Lane parity eval — ``claude_sdk`` ↔ ``pydantic_ai`` yield the same vocabulary.

The PR-03 acceptance criteria, proven with zero tokens.

(a) A scripted ``FunctionModel`` trajectory (``ALLOW_MODEL_REQUESTS = False``)
drives the REAL :class:`PydanticAiHarness` session end-to-end, and the messages
it yields are the SAME ``claude_agent_sdk`` vocabulary the ``claude_sdk`` lane
yields — a tool call surfaces as a :class:`ToolUseBlock`, its result as a
:class:`ToolResultBlock`, the final text as a :class:`TextBlock`, closed by a
:class:`ResultMessage`.

(c) A hard-deny gate (main-clone mutation) fires on Lane B through the SAME
shared :func:`hard_deny_reason` the ``claude_sdk`` lane's PreToolUse hook
consults, surfacing an ``is_error`` :class:`ToolResultBlock`.
"""

import asyncio
import json
from pathlib import Path

import pydantic_ai.models
import pytest
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)
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
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from teatree.agents.harness import PydanticAiHarness
from teatree.agents.lane_b.gating import hard_deny_reason
from teatree.hooks.quote_scanner import extract_publish_payload, scan_text
from tests.teatree_agents.lane_b._managed_clone import linked_worktree, managed_main_clone

pydantic_ai.models.ALLOW_MODEL_REQUESTS = False  # ty: ignore[invalid-assignment] — the zero-token test guard.


def _streaming_model(*, tool_command: str) -> FunctionModel:
    """A streaming FunctionModel: call ``shell`` with *tool_command*, then text."""
    state = {"n": 0}

    def stream_fn(messages: object, info: object) -> object:
        state["n"] += 1
        turn = state["n"]

        async def gen():  # noqa: RUF029 — an async generator (the stream contract) that only yields.
            if turn == 1:
                args = json.dumps({"command": tool_command})
                yield {0: DeltaToolCall(name="shell", json_args=args, tool_call_id="c1")}
            else:
                yield "done"

        return gen()

    return FunctionModel(stream_function=stream_fn)


def _collect(harness: PydanticAiHarness, options: ClaudeAgentOptions, prompt: str) -> list[object]:
    async def run() -> list[object]:
        async with harness.open(options) as session:
            await session.query(prompt)
            return [message async for message in session.receive_response()]

    return asyncio.run(run())


def _blocks(messages: list[object], block_type: type) -> list[object]:
    return [
        block
        for message in messages
        if isinstance(message, AssistantMessage)
        for block in message.content
        if isinstance(block, block_type)
    ]


class TestVocabularyParity:
    def test_safe_tool_call_yields_the_sdk_message_vocabulary(self, tmp_path: Path) -> None:
        (tmp_path / "marker").write_text("")
        harness = PydanticAiHarness(model=_streaming_model(tool_command="ls"), phase="coding")
        messages = _collect(harness, ClaudeAgentOptions(cwd=str(tmp_path)), "list the dir")

        # Every yielded message is a claude_agent_sdk type — the seam's contract.
        assert all(isinstance(m, (AssistantMessage, ResultMessage)) for m in messages)
        # The tool call + its result surfaced in the seam's tool-block vocabulary.
        tool_uses = _blocks(messages, ToolUseBlock)
        assert [t.name for t in tool_uses] == ["shell"]
        tool_results = _blocks(messages, ToolResultBlock)
        assert tool_results
        assert any("marker" in str(r.content) for r in tool_results)
        # The final text + a terminal ResultMessage.
        assert [b.text for b in _blocks(messages, TextBlock)] == ["done"]
        assert isinstance(messages[-1], ResultMessage)


class TestHardDenyParity:
    def test_main_clone_mutation_is_refused_when_cwd_is_a_managed_main_clone(self, tmp_path: Path) -> None:
        command = "git reset --hard HEAD~1"
        clone = managed_main_clone(tmp_path / "teatree")
        # The shared evaluator the claude_sdk lane's PreToolUse hook also consults.
        assert hard_deny_reason("shell", {"command": command}, cwd=clone) is not None

        harness = PydanticAiHarness(model=_streaming_model(tool_command=command), phase="coding")
        messages = _collect(harness, ClaudeAgentOptions(cwd=str(clone)), "reset hard")

        error_results = [r for r in _blocks(messages, ToolResultBlock) if r.is_error]
        assert error_results, "a refused tool call must surface an is_error ToolResultBlock"
        assert any("BLOCKED" in str(r.content) for r in error_results)

    def test_same_mutation_runs_in_a_linked_worktree(self, tmp_path: Path) -> None:
        # The Lane-B jail root is the WORKTREE, so the same op Lane A allows there
        # is NOT refused: no is_error ToolResultBlock, and the command executes.
        command = "git reset --hard HEAD"
        clone = managed_main_clone(tmp_path / "teatree")
        wt = linked_worktree(clone, tmp_path / "wt")
        assert hard_deny_reason("shell", {"command": command}, cwd=wt) is None

        harness = PydanticAiHarness(model=_streaming_model(tool_command=command), phase="coding")
        messages = _collect(harness, ClaudeAgentOptions(cwd=str(wt)), "reset soft")

        assert not [r for r in _blocks(messages, ToolResultBlock) if r.is_error]


def _pairing_validator_model(orphans: list[str]) -> FunctionModel:
    """A streaming FunctionModel that records every orphaned tool-result it is sent.

    A ``ToolReturnPart`` / tool-linked ``RetryPromptPart`` whose ``tool_call_id``
    was not produced by a preceding ``ToolCallPart`` is exactly the "tool message
    without preceding tool_calls" an OpenAI-compatible provider rejects — the model
    stand-in for that wire-level check.
    """

    def stream_fn(messages: object, info: object) -> object:
        call_ids: set[str] = set()
        for message in messages:  # type: ignore[attr-defined]
            if isinstance(message, ModelResponse):
                call_ids.update(p.tool_call_id for p in message.parts if isinstance(p, ToolCallPart))
            elif isinstance(message, ModelRequest):
                for part in message.parts:
                    tool_linked_retry = isinstance(part, RetryPromptPart) and part.tool_name is not None
                    if (isinstance(part, ToolReturnPart) or tool_linked_retry) and part.tool_call_id not in call_ids:
                        orphans.append(part.tool_call_id)

        async def gen():  # noqa: RUF029 — an async generator (the stream contract) that only yields.
            yield "ok"

        return gen()

    return FunctionModel(stream_function=stream_fn)


def _history_straddling_a_tool_pair() -> list[ModelMessage]:
    """A 44-message history whose default (``keep_recent=40``) cut orphans a return.

    The kept window is the last 40 (indices 4..43): the tool RETURN sits at index 4
    (first kept) while its CALL at index 3 falls in the dropped middle, so a naive
    ``[first, *last-40]`` keeps an orphaned return. Index 5 onward is filler so the
    snapped window opens on a non-tool message.
    """
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="task")]),  # 0: framing
        ModelResponse(parts=[TextPart(content="a1")]),  # 1: dropped middle
        ModelRequest(parts=[UserPromptPart(content="u2")]),  # 2: dropped middle
        ModelResponse(parts=[ToolCallPart(tool_name="shell", args={"command": "ls"}, tool_call_id="c1")]),  # 3: CALL
        ModelRequest(parts=[ToolReturnPart(tool_name="shell", content="out", tool_call_id="c1")]),  # 4: RETURN
    ]
    for i in range(5, 44):
        if i % 2 == 1:
            history.append(ModelResponse(parts=[TextPart(content=f"a{i}")]))
        else:
            history.append(ModelRequest(parts=[UserPromptPart(content=f"u{i}")]))
    return history


class TestCompactionRoundTrip:
    def test_compacted_history_round_trips_with_no_orphaned_tool_return(self, tmp_path: Path) -> None:
        # The REAL PydanticAiHarness compacts the seeded history before the turn
        # (phase set → Lane-B tool layer + compaction). A validator FunctionModel
        # stands in for the OpenAI-compatible provider's tool-pairing check.
        orphans: list[str] = []
        harness = PydanticAiHarness(
            model=_pairing_validator_model(orphans),
            history=_history_straddling_a_tool_pair(),
            phase="coding",
        )
        _collect(harness, ClaudeAgentOptions(cwd=str(tmp_path)), "continue")

        assert orphans == [], f"the compacted history sent to the model orphaned a tool-return: {orphans}"


class TestPrivacyGateParity:
    """The privacy/banned-term gate refuses the SAME publish set on both lanes.

    Lane A's PreToolUse scopes the scan to :func:`extract_publish_payload` (``None``
    for a non-publish call); Lane B's :func:`hard_deny_reason` now uses the same
    scoping, so a local write / non-publish shell command is refused on NEITHER
    lane, and a publish command carrying a HIGH finding is refused on BOTH.
    """

    _HIGH_BODY = "the user said: do it now"  # trips the ``the-user-said-colon`` HIGH pattern

    def _lane_a_denies(self, tool_name: str, tool_args: dict, cwd: Path | None) -> bool:
        # Lane A's ground truth: a publish payload (else None) run through the same scan.
        command = tool_args.get("command", "") if tool_name == "shell" else ""
        payload = extract_publish_payload("Bash", {"command": command}, cwd) if command else None
        return payload is not None and scan_text(payload).has_high

    def test_local_write_with_a_high_finding_is_refused_on_neither_lane(self, tmp_path: Path) -> None:
        # RED without the fix: Lane B scanned every string arg, so write_file's
        # content tripped HIGH and was denied while Lane A never scans a local write.
        args = {"path": "note.md", "content": self._HIGH_BODY}
        assert hard_deny_reason("write_file", args, cwd=tmp_path) is None
        assert self._lane_a_denies("write_file", args, tmp_path) is False

    def test_non_publish_shell_command_with_a_high_finding_is_refused_on_neither_lane(self, tmp_path: Path) -> None:
        # A local `echo ... > file` is not a publish — Lane A passes it through, and
        # Lane B must too (RED without the fix: the whole command string was scanned).
        args = {"command": f'echo "{self._HIGH_BODY}" > note.md'}
        assert hard_deny_reason("shell", args, cwd=tmp_path) is None
        assert self._lane_a_denies("shell", args, tmp_path) is False

    def test_publish_command_with_a_high_finding_is_refused_on_both_lanes(self, tmp_path: Path) -> None:
        args = {"command": f'gh pr comment 5 --body "{self._HIGH_BODY}"'}
        reason = hard_deny_reason("shell", args, cwd=tmp_path)
        assert reason is not None
        assert "privacy/banned-term gate" in reason
        assert self._lane_a_denies("shell", args, tmp_path) is True

    def test_clean_publish_command_is_refused_on_neither_lane(self, tmp_path: Path) -> None:
        args = {"command": 'gh pr comment 5 --body "shipped the compaction fix"'}
        assert hard_deny_reason("shell", args, cwd=tmp_path) is None
        assert self._lane_a_denies("shell", args, tmp_path) is False


def test_zero_tokens_enforced() -> None:
    assert pydantic_ai.models.ALLOW_MODEL_REQUESTS is False


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])
