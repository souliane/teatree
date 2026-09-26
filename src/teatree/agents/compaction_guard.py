"""Keep a factory run's history uncompacted, and end the run when Claude Code tries to compact it.

A compaction rewrites the transcript the run is working from; the factory re-dispatches a fresh
session from its durable handoff instead. :data:`COMPACTION_SWITCH_ENV` switches compaction off for
the spawned ``claude`` child. :class:`CompactionGuard` is the ``PreCompact`` fallback for a CLI that
compacts anyway: it blocks every compaction, and an automatic one also stops the run, so the runner
records it and the requeue sweep re-dispatches it fresh. Every factory-headless composer gets both
through :func:`with_compaction_off`.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from claude_agent_sdk.types import HookCallback, HookContext, HookJSONOutput, HookMatcher, PreCompactHookInput

from teatree.agents.runner_failure_taxonomy import RESULT_ERROR_PREFIX
from teatree.core.modelkit.task_failure_taxonomy import COMPACTION_BLOCKED_MARKER

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

    from teatree.agents.harness import Harness, HarnessSession
    from teatree.agents.harness_registry import HarnessCapabilities

logger = logging.getLogger(__name__)

COMPACTION_SWITCH_ENV: Mapping[str, str] = MappingProxyType({"DISABLE_COMPACT": "1"})

COMPACTION_BLOCKED_REASON = (
    f"{RESULT_ERROR_PREFIX}{COMPACTION_BLOCKED_MARKER}Claude Code tried to auto-compact the run's history; "
    "the compaction was blocked and the run ended so it re-dispatches as a fresh session"
)

type StopRun = Callable[[], Awaitable[None]]


class CompactionGuard:
    """One factory run's ``PreCompact`` tripwire: the triggers it blocked, and how it stops the run."""

    def __init__(self) -> None:
        self.triggers: list[str] = []
        self._stop: StopRun | None = None
        self._stopping: set[asyncio.Task[None]] = set()

    @property
    def stopped_run(self) -> bool:
        return "auto" in self.triggers

    def arm(self, stop: StopRun) -> None:
        self._stop = stop

    async def pre_compact(
        self,
        input_data: PreCompactHookInput,
        tool_use_id: str | None,
        context: HookContext,
    ) -> HookJSONOutput:
        del tool_use_id, context
        trigger = input_data["trigger"]
        self.triggers.append(trigger)
        logger.error(
            "Claude Code tried to compact a factory run (trigger=%s, session=%s); blocking it",
            trigger,
            input_data["session_id"],
        )
        reason = f"teatree keeps factory runs uncompacted (trigger={trigger})"
        if trigger != "auto":
            return {"decision": "block", "reason": reason}
        self._stop_run()
        return {"decision": "block", "reason": reason, "continue_": False, "stopReason": reason}

    def _stop_run(self) -> None:
        if self._stop is None:
            return
        # A blocked compaction alone lets the CLI carry on uncompacted, so the run is interrupted too.
        stopping = asyncio.get_running_loop().create_task(self._stop_quietly(self._stop))
        self._stopping.add(stopping)
        stopping.add_done_callback(self._stopping.discard)

    @staticmethod
    async def _stop_quietly(stop: StopRun) -> None:
        try:
            await stop()
        except Exception:
            logger.warning("could not interrupt a run after blocking its compaction", exc_info=True)


@dataclass(frozen=True, slots=True)
class GuardedHarness:
    """A harness whose opened sessions the compaction guard can interrupt."""

    harness: "Harness"
    guard: CompactionGuard

    @property
    def capabilities(self) -> "HarnessCapabilities":
        return self.harness.capabilities

    @asynccontextmanager
    async def open(self, options: "ClaudeAgentOptions") -> "AsyncIterator[HarnessSession]":
        async with self.harness.open(options) as session:
            self.guard.arm(session.interrupt)
            yield session


def with_compaction_off(options: "ClaudeAgentOptions", guard: CompactionGuard | None = None) -> "ClaudeAgentOptions":
    """*options* with compaction switched off for their ``claude`` child, and *guard* (or a fresh one) behind it."""
    options.env = {**(options.env or {}), **COMPACTION_SWITCH_ENV}
    callback = cast("HookCallback", (guard or CompactionGuard()).pre_compact)
    options.hooks = {**(options.hooks or {}), "PreCompact": [HookMatcher(hooks=[callback])]}
    return options
