"""A deterministic ceiling on how many sub-agents one headless run may spawn.

The frontier model teatree dispatches on is documented by its vendor as reaching
for sub-agents more freely than its predecessor, with the explicit instruction to
add a deterministic ceiling on spawn count — a prose "delegate less" instruction
is not one. This is that ceiling: a ``PreToolUse`` hook that counts delegation
tool calls for the run and refuses the ones past :attr:`SpawnCeiling.limit`.

**What it adds over the armed runtime watchdog.** ``watchdog_max_runtime_seconds``
bounds a run's DURATION, not its spend RATE — a fan-out multiplies both cost and
tokens well inside three hours, and a wall clock cannot see that. This bounds the
multiplier itself. It also fires precisely (one refused tool call) where the
watchdog fires bluntly (interrupt the agent, record a ``stuck_loop`` failure), so
it never strands in-flight work.

**It refuses a spawn, never the run.** The verdict is ``deny`` on the single tool
call: sub-agents already running finish, the parent keeps its context and its
remaining turns, and the refusal text tells it to complete the work in its own
loop — which is what the vendor guidance asks for anyway. There is no path here
that halts a turn or ends a run.

**It is loud.** The refusal carries a reason the agent reads, a ``systemMessage``
the operator sees in the transcript, a ``WARNING`` log line, and a counter on the
instance that outlives both. A cap nobody can see is the defect class this avoids.
"""

import logging
from typing import Any, cast

from claude_agent_sdk.types import HookCallback, HookContext, HookJSONOutput, HookMatcher, PreToolUseHookInput

from teatree.agents.sdk_tool_map import CAPABILITY_TO_SDK_TOOLS

logger = logging.getLogger(__name__)

#: The vendor's own named bound on parallel agents. Applied here CUMULATIVELY over
#: one run, which is strictly tighter: twenty parallel agents per wave, repeated,
#: is the runaway this exists to bound. Twenty is roughly double the widest fan-out
#: any recorded dispatch has needed, so it refuses runaways without refusing work.
DEFAULT_SPAWN_CEILING = 20

#: The SDK tool names the ``dispatch_subtask`` capability grants, as an SDK hook
#: matcher. Derived from the capability map rather than spelled out, so a change to
#: what delegation means cannot leave this gate matching a stale tool name.
SPAWN_TOOL_MATCHER = "|".join(sorted(CAPABILITY_TO_SDK_TOOLS["dispatch_subtask"]))

_SPAWN_TOOLS = frozenset(CAPABILITY_TO_SDK_TOOLS["dispatch_subtask"])


class SpawnCeiling:
    """Counts one run's sub-agent spawns and refuses the ones past *limit*.

    One instance per headless dispatch — the count is the run's, and a fresh
    dispatch starts from zero. A *limit* of ``0`` (or below) disables the gate
    entirely, matching the ``0 = disabled`` convention the watchdog ceilings use.
    """

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.spawns = 0
        self.refused = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    @property
    def breached(self) -> bool:
        """Whether this run ever asked for a spawn past the ceiling."""
        return self.refused > 0

    async def pre_tool_use(
        self,
        input_data: PreToolUseHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        """Allow or refuse one tool call; only a delegation call is ever counted."""
        del tool_use_id, context
        if not self.enabled or input_data["tool_name"] not in _SPAWN_TOOLS:
            return {}
        if self.spawns < self.limit:
            self.spawns += 1
            return {}
        self.refused += 1
        return self._refusal()

    def _refusal(self) -> HookJSONOutput:
        reason = (
            f"Sub-agent spawn ceiling reached: this run has already dispatched {self.spawns} "
            f"sub-agents, and the ceiling is {self.limit}. Do not delegate again — complete the "
            "remaining work yourself in this loop. Sub-agents already running are unaffected."
        )
        logger.warning(
            "sub-agent spawn ceiling refused a dispatch: spawns=%d limit=%d refused=%d",
            self.spawns,
            self.limit,
            self.refused,
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            },
            "systemMessage": (
                f"teatree refused a sub-agent dispatch: {self.spawns} spawned, ceiling {self.limit} "
                f"({self.refused} refused so far). Raise `subagent_spawn_ceiling` if this run needs more."
            ),
        }


def spawn_ceiling_hooks(ceiling: SpawnCeiling) -> dict[Any, list[HookMatcher]]:
    """The ``ClaudeAgentOptions.hooks`` bundle that arms *ceiling* on a dispatch."""
    # The SDK's HookCallback takes the whole HookInput union; this callback is
    # registered on PreToolUse alone, so its narrower parameter is sound here.
    callback = cast("HookCallback", ceiling.pre_tool_use)
    return {"PreToolUse": [HookMatcher(matcher=SPAWN_TOOL_MATCHER, hooks=[callback])]}
