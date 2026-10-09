"""A phase task stops at its next round boundary once it has worked 90 minutes or pushed three rounds.

A shipping task ran four hours of fix / verify-gates / push / red-pipeline rounds against a
moving main, holding its claim — and a deploy drain — the whole time. The ceiling holds on
every runtime: a hook runtime has the round start refused before it runs, and a runtime
without hooks is stopped by the driver as the round starts.
"""

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest
from claude_agent_sdk import AssistantMessage, SystemMessage, ToolResultBlock, ToolUseBlock, UserMessage

from teatree.agents.round_ceiling import (
    PUSHED_ROUND_CEILING,
    ROUND_STARTED,
    WORK_CEILING_SECONDS,
    RoundCeiling,
    is_a_push,
    starts_a_round,
)
from tests.teatree_agents import _codex_command_shape as _codex_shape

if TYPE_CHECKING:
    from claude_agent_sdk.types import HookContext, PreToolUseHookInput


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _hook(ceiling: RoundCeiling, command: str, tool: str = "Bash") -> dict[str, Any]:
    payload = cast("PreToolUseHookInput", {"tool_name": tool, "tool_input": {"command": command}})
    return cast("dict[str, Any]", asyncio.run(ceiling.pre_tool_use(payload, None, cast("HookContext", {}))))


def _refused(verdict: dict[str, Any]) -> bool:
    return verdict.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _ran(ceiling: RoundCeiling, command: str, *, tool_id: str, failed: bool = False) -> None:
    ceiling.observe(
        AssistantMessage(content=[ToolUseBlock(id=tool_id, name="Bash", input={"command": command})], model="m")
    )
    ceiling.observe(UserMessage(content=[ToolResultBlock(tool_use_id=tool_id, content="", is_error=failed)]))


def _push_rounds(ceiling: RoundCeiling, rounds: int) -> None:
    for n in range(rounds):
        _ran(ceiling, "git push origin HEAD", tool_id=f"push-{n}")


@pytest.mark.parametrize(
    "command",
    [
        "git push",
        "git push origin HEAD",
        "git -C /work/tree push origin feature",
        "git -c push.default=current push",
        "git --git-dir=/w/.git --work-tree /w push",
        "cd /work/tree && git push 2>&1 | tail -5",
        "timeout 600 git push",
        "env GIT_TRACE=1 git push",
        "FOO=1 git -C /w push",
        "bash -c 'git -C /w push origin b'",
        "/bin/zsh -lc 'git -C /w push origin b'",
        'bash -lc "t3 demo push --repo /w"',
        "sh -xc 'git push'",
        "bash -e -c 'git push'",
        "bash -lic 'git push'",
        "bash -o pipefail -c 'git push 2>&1 | tail -3'",
        "t3 push",
        "t3 teatree tool verify-gates --all",
        "uv run t3 tool verify-gates",
    ],
)
def test_every_spelling_of_a_round_start_is_recognised(command: str) -> None:
    assert starts_a_round(command)


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "echo t3 push notes",
        "git log --grep push",
        "git commit -m 'push later'",
        "git -C push status",
        "grep -rn verify-gates docs",
        "cat 'unterminated",
        "bash -l ./push.sh",
        "/bin/zsh -lc 'git status'",
    ],
)
def test_commands_that_only_mention_a_push_are_not_round_starts(command: str) -> None:
    assert not starts_a_round(command)


def test_the_shape_the_pinned_codex_reports_is_parsed_through_its_shell_wrapper() -> None:
    assert _codex_shape.CAPTURED_COMMAND.startswith("/bin/")
    assert _codex_shape.codex_wrapped(_codex_shape.CAPTURED_INNER) == _codex_shape.CAPTURED_COMMAND
    assert starts_a_round(_codex_shape.codex_wrapped("git -C /work push origin feature"))
    assert is_a_push(_codex_shape.codex_wrapped("git push"))
    assert not starts_a_round(_codex_shape.CAPTURED_COMMAND)


class TestTheHookRefusesTheRoundStart:
    def test_rounds_inside_both_ceilings_proceed(self) -> None:
        ceiling = RoundCeiling(enforce_by_observation=False, clock=_Clock())

        assert not _refused(_hook(ceiling, "t3 tool verify-gates"))
        assert not _refused(_hook(ceiling, "git push origin HEAD"))

    def test_the_push_after_the_last_allowed_round_is_refused_with_the_handoff(self) -> None:
        ceiling = RoundCeiling(enforce_by_observation=False, clock=_Clock())
        _push_rounds(ceiling, PUSHED_ROUND_CEILING)

        verdict = _hook(ceiling, "git -C /w push")

        assert _refused(verdict)
        reason = verdict["hookSpecificOutput"]["permissionDecisionReason"]
        assert "needs_user_input" in reason
        assert f"{PUSHED_ROUND_CEILING} rounds" in reason

    def test_the_handoff_never_promises_a_resume_a_kindless_stop_does_not_get(self) -> None:
        ceiling = RoundCeiling(enforce_by_observation=False, clock=_Clock())
        _push_rounds(ceiling, PUSHED_ROUND_CEILING)

        reason = _hook(ceiling, "git push")["hookSpecificOutput"]["permissionDecisionReason"]

        assert "resumes from here" not in reason
        assert "nothing resumes on its own" in reason

    def test_a_refused_or_failed_push_is_not_a_round(self) -> None:
        ceiling = RoundCeiling(enforce_by_observation=False, clock=_Clock())
        _push_rounds(ceiling, PUSHED_ROUND_CEILING - 1)
        _ran(ceiling, "git push", tool_id="denied", failed=True)

        assert not _refused(_hook(ceiling, "git push"))

    def test_a_new_round_after_ninety_minutes_is_refused(self) -> None:
        clock = _Clock()
        ceiling = RoundCeiling(enforce_by_observation=False, clock=clock)
        clock.now += WORK_CEILING_SECONDS + 1

        assert _refused(_hook(ceiling, "t3 teatree tool verify-gates --all"))
        assert _refused(_hook(ceiling, "git -c k=v push"))

    def test_work_inside_a_round_is_never_cut_off(self) -> None:
        clock = _Clock()
        ceiling = RoundCeiling(enforce_by_observation=False, clock=clock)
        clock.now += WORK_CEILING_SECONDS + 1

        assert not _refused(_hook(ceiling, "uv run pytest tests/test_x.py"))
        assert not _refused(_hook(ceiling, "git push", tool="Read"))

    def test_a_hook_runtime_is_never_stopped_by_observation(self) -> None:
        ceiling = RoundCeiling(enforce_by_observation=False, clock=_Clock())
        _push_rounds(ceiling, PUSHED_ROUND_CEILING + 1)

        assert ceiling.handoff == ""


class TestARuntimeWithoutHooksIsStoppedAsTheRoundStarts:
    def test_a_round_starting_past_the_push_ceiling_asks_for_the_handoff(self) -> None:
        ceiling = RoundCeiling(enforce_by_observation=True, clock=_Clock())
        _push_rounds(ceiling, PUSHED_ROUND_CEILING)
        assert ceiling.handoff == ""

        ceiling.observe(SystemMessage(subtype=ROUND_STARTED, data={"command": "git -C /w push"}))

        assert f"{PUSHED_ROUND_CEILING} rounds" in ceiling.handoff

    def test_a_round_starting_past_ninety_minutes_asks_for_the_handoff(self) -> None:
        clock = _Clock()
        ceiling = RoundCeiling(enforce_by_observation=True, clock=clock)
        clock.now += WORK_CEILING_SECONDS + 1

        ceiling.observe(
            AssistantMessage(
                content=[ToolUseBlock(id="v", name="Bash", input={"command": "t3 tool verify-gates"})], model="m"
            )
        )

        assert "minutes" in ceiling.handoff

    def test_other_commands_never_ask_for_it(self) -> None:
        clock = _Clock()
        ceiling = RoundCeiling(enforce_by_observation=True, clock=clock)
        clock.now += WORK_CEILING_SECONDS + 1

        ceiling.observe(SystemMessage(subtype=ROUND_STARTED, data={"command": "uv run pytest"}))

        assert ceiling.handoff == ""


def test_ninety_minutes_is_the_owner_ceiling_and_three_rounds_the_other() -> None:
    assert WORK_CEILING_SECONDS == 90 * 60
    assert PUSHED_ROUND_CEILING == 3
