"""The deterministic per-run sub-agent spawn ceiling.

The properties that matter are the ones a silent cap would violate: the breach is
legible to the agent AND recorded for the operator, it denies only the new spawn
so nothing in flight is stranded, and a ceiling of ``0`` turns the whole gate off.
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest

from teatree.agents.sdk_tool_map import CAPABILITY_TO_SDK_TOOLS
from teatree.agents.subagent_ceiling import DEFAULT_SPAWN_CEILING, SPAWN_TOOL_MATCHER, SpawnCeiling, spawn_ceiling_hooks

if TYPE_CHECKING:
    from claude_agent_sdk.types import PreToolUseHookInput

_SPAWN_TOOL = "Task"


def _decide(ceiling: SpawnCeiling, tool_name: str = _SPAWN_TOOL) -> dict[str, Any]:
    hook_input = cast("PreToolUseHookInput", {"tool_name": tool_name})
    return cast("dict[str, Any]", asyncio.run(ceiling.pre_tool_use(hook_input, None, {"signal": None})))


def _verdict(decision: dict[str, Any]) -> str:
    """``allow`` is the ABSENCE of an opinion — the gate only ever subtracts.

    Returning an explicit ``permissionDecision: "allow"`` would have this hook
    grant a tool call the rest of the permission chain might refuse. A gate whose
    job is to deny must never widen, so the allow path returns nothing at all.
    """
    specific = decision.get("hookSpecificOutput")
    if specific is None:
        assert decision == {}
        return "allow"
    assert isinstance(specific, dict)
    return str(specific.get("permissionDecision"))


class TestMatcher:
    def test_matcher_is_derived_from_the_delegation_capability_not_hard_coded(self) -> None:
        assert set(SPAWN_TOOL_MATCHER.split("|")) == set(CAPABILITY_TO_SDK_TOOLS["dispatch_subtask"])

    def test_every_matched_name_is_a_delegation_tool(self) -> None:
        assert SPAWN_TOOL_MATCHER.split("|")


class TestAllowsWorkUpToTheCeiling:
    def test_every_spawn_up_to_the_ceiling_is_allowed(self) -> None:
        ceiling = SpawnCeiling(limit=3)
        assert [_verdict(_decide(ceiling)) for _ in range(3)] == ["allow", "allow", "allow"]

    def test_a_fresh_dispatch_starts_from_zero(self) -> None:
        spent = SpawnCeiling(limit=1)
        _decide(spent)
        assert _verdict(_decide(spent)) == "deny"
        assert _verdict(_decide(SpawnCeiling(limit=1))) == "allow"

    def test_a_non_delegation_tool_is_neither_counted_nor_denied(self) -> None:
        ceiling = SpawnCeiling(limit=1)
        for _ in range(5):
            assert _verdict(_decide(ceiling, tool_name="Bash")) == "allow"
        assert ceiling.spawns == 0
        assert _verdict(_decide(ceiling)) == "allow"


class TestDeniesBeyondTheCeiling:
    def test_the_spawn_past_the_ceiling_is_denied(self) -> None:
        ceiling = SpawnCeiling(limit=2)
        _decide(ceiling)
        _decide(ceiling)
        assert _verdict(_decide(ceiling)) == "deny"

    def test_the_denial_reason_names_the_ceiling_and_the_count(self) -> None:
        ceiling = SpawnCeiling(limit=2)
        _decide(ceiling)
        _decide(ceiling)
        specific = _decide(ceiling)["hookSpecificOutput"]
        assert isinstance(specific, dict)
        reason = str(specific["permissionDecisionReason"])
        assert "2" in reason
        assert "ceiling" in reason.lower()

    def test_the_denial_tells_the_agent_to_finish_the_work_itself(self) -> None:
        ceiling = SpawnCeiling(limit=1)
        _decide(ceiling)
        specific = _decide(ceiling)["hookSpecificOutput"]
        assert isinstance(specific, dict)
        assert "yourself" in str(specific["permissionDecisionReason"]).lower()

    def test_the_denial_carries_an_operator_visible_system_message(self) -> None:
        ceiling = SpawnCeiling(limit=1)
        _decide(ceiling)
        assert str(_decide(ceiling)["systemMessage"])

    def test_the_breach_is_logged_at_warning_so_it_is_never_silent(self, caplog: pytest.LogCaptureFixture) -> None:
        ceiling = SpawnCeiling(limit=1)
        _decide(ceiling)
        with caplog.at_level(logging.WARNING, logger="teatree.agents.subagent_ceiling"):
            _decide(ceiling)
        assert [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_the_breach_is_recorded_on_the_ceiling_so_it_outlives_the_log(self) -> None:
        ceiling = SpawnCeiling(limit=1)
        _decide(ceiling)
        assert not ceiling.breached
        _decide(ceiling)
        _decide(ceiling)
        assert ceiling.breached
        assert ceiling.refused == 2


class TestDoesNotStrandInFlightWork:
    def test_a_denial_never_halts_the_run(self) -> None:
        ceiling = SpawnCeiling(limit=1)
        _decide(ceiling)
        decision = _decide(ceiling)
        assert decision.get("continue_") is not False
        assert "stopReason" not in decision

    def test_a_denial_never_blocks_the_turn(self) -> None:
        ceiling = SpawnCeiling(limit=1)
        _decide(ceiling)
        assert _decide(ceiling).get("decision") != "block"


class TestKillSwitch:
    def test_a_ceiling_of_zero_allows_every_spawn(self) -> None:
        ceiling = SpawnCeiling(limit=0)
        assert [_verdict(_decide(ceiling)) for _ in range(50)] == ["allow"] * 50
        assert not ceiling.breached

    def test_a_negative_ceiling_is_treated_as_disabled_not_as_deny_everything(self) -> None:
        assert _verdict(_decide(SpawnCeiling(limit=-1))) == "allow"


class TestHookWiring:
    def test_the_hook_bundle_registers_the_delegation_matcher_on_pretooluse(self) -> None:
        ceiling = SpawnCeiling(limit=DEFAULT_SPAWN_CEILING)
        hooks = spawn_ceiling_hooks(ceiling)
        matchers = hooks["PreToolUse"]
        assert [m.matcher for m in matchers] == [SPAWN_TOOL_MATCHER]
        assert matchers[0].hooks == [ceiling.pre_tool_use]

    def test_the_shipped_default_is_the_vendor_named_parallel_agent_bound(self) -> None:
        assert DEFAULT_SPAWN_CEILING == 20
