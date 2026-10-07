"""Lease heartbeat and watchdog driving for one open harness session."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.harness import Harness, HarnessSession
from teatree.agents.live_mailbox import bound_task_mailbox
from teatree.agents.round_ceiling import RoundCeiling
from teatree.agents.runner_stream import HarnessOutcome, StreamCapture, _collect
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.managers_task_claim import drain_block_reason
from teatree.core.models import LeaseLostError, Task
from teatree.core.models.task_claim import describe_lease_loss
from teatree.core.worktree.occupancy import WorktreeOccupancyLostError, renew_ticket_checkout, task_holder_id
from teatree.utils.thread_db import close_thread_db_connections

logger = logging.getLogger(__name__)

UsageSampler = Callable[[Task], TaskUsage]
LeaseRenewer = Callable[[Task], None]

#: Beats a drain waits on an open tool call, so a push or merge is not cut in half yet the wait fits the drain grace.
CHECKPOINT_MAX_DEFER_BEATS = 3


@dataclass(frozen=True, slots=True)
class HeartbeatRuntime:
    watchdog: LoopWatchdog
    heartbeat_interval: float
    sample_usage: UsageSampler
    renew_lease: LeaseRenewer
    drain_reason: Callable[[], str]


def renew_lease_closing_connection(task: Task, *, lease_seconds: int) -> None:
    """Renew task/worktree leases and close this worker thread's DB connection."""
    try:
        task.renew_lease(lease_seconds=lease_seconds)
        if task.ticket_id is not None:  # ty: ignore[unresolved-attribute]  # Django FK accessor
            renew_ticket_checkout(task.ticket, holder=task_holder_id(task), holder_session=task.claimed_by_session)
    except LeaseLostError as exc:
        raise LeaseLostError(describe_lease_loss(task)) from exc
    except WorktreeOccupancyLostError as exc:
        msg = f"lease lost for task {task.pk}: {exc}"
        raise LeaseLostError(msg) from exc
    finally:
        close_thread_db_connections()


def drain_reason_closing_connection() -> str:
    try:
        return drain_block_reason()
    finally:
        close_thread_db_connections()


def _arm_round_ceiling(options: ClaudeAgentOptions, harness: Harness) -> RoundCeiling:
    """A hook runtime has the round start refused; any other is stopped by the driver as the round starts."""
    takes_hooks = getattr(getattr(harness, "harness", harness), "capabilities", None)
    ceiling = RoundCeiling(enforce_by_observation=not (takes_hooks is not None and takes_hooks.hooks))
    if not ceiling.enforces_by_observation:
        hooks = dict(options.hooks or {})
        hooks["PreToolUse"] = [*hooks.get("PreToolUse", []), ceiling.matcher()]
        options.hooks = hooks
    return ceiling


async def drive_with_heartbeat(
    task: Task,
    prompt: str,
    options: ClaudeAgentOptions,
    harness: Harness,
    *,
    runtime: HeartbeatRuntime,
) -> HarnessOutcome:
    """Drive one session while renewing ownership and enforcing watchdog ceilings."""
    underlying = getattr(harness, "harness", harness)
    with bound_task_mailbox(
        options,
        room=str(task.ticket_id),  # ty: ignore[unresolved-attribute] — Django FK attname
        harness=type(underlying).__name__,
        label=f"{task.phase} task {task.pk}",
    ):
        return await _drive_bound_session(task, prompt, options, harness, runtime=runtime)


@dataclass
class _Heartbeat:
    """One run's beat: renew its lease, then interrupt it on a lost lease, a deploy drain or a watchdog breach."""

    task: Task
    runtime: HeartbeatRuntime
    session: HarnessSession
    capture: StreamCapture
    usage: TaskUsage
    started_at: float
    breach: list[str] = field(default_factory=list)
    lease_lost: bool = False
    checkpointed: bool = False
    deferred_beats: int = 0

    async def run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.runtime.heartbeat_interval)
                if await self._lost_the_lease():
                    return
                # An interrupted run holds its claim until it ends, so only the lease is renewed from here on.
                if self.breach or await self._checkpointed():
                    continue
                await self._breached()
        finally:
            await asyncio.to_thread(close_thread_db_connections)

    def folded(self, outcome: HarnessOutcome) -> HarnessOutcome:
        if self.breach and outcome.stuck_reason is None:
            return replace(
                outcome, stuck_reason=self.breach[0], lease_lost=self.lease_lost, checkpointed=self.checkpointed
            )
        return outcome

    async def _interrupt(self, reason: str) -> bool:
        self.breach.append(reason)
        await self.session.interrupt()
        return True

    async def _lost_the_lease(self) -> bool:
        try:
            await asyncio.to_thread(self.runtime.renew_lease, self.task)
        except LeaseLostError as exc:
            # The flags name what FIRST interrupted the run; a claim lost after that only ends the beat.
            self.lease_lost = not self.breach
            logger.warning("Task %s lease lost; interrupting duplicate run", self.task.pk)
            return await self._interrupt(str(exc))
        except Exception:
            logger.warning("Heartbeat failed for task %s", self.task.pk, exc_info=True)
        return False

    async def _checkpointed(self) -> bool:
        try:
            drain = await asyncio.to_thread(self.runtime.drain_reason)
        except Exception:
            logger.warning("Drain check failed for task %s; the run continues", self.task.pk, exc_info=True)
            return False
        if not drain:
            return False
        if self.capture.tool_in_flight and self.deferred_beats < CHECKPOINT_MAX_DEFER_BEATS:
            self.deferred_beats += 1
            return False
        self.checkpointed = True
        logger.warning("Task %s checkpointing for a deploy drain: %s", self.task.pk, drain)
        return await self._interrupt(f"deploy checkpoint: {drain}")

    async def _breached(self) -> bool:
        watchdog = self.runtime.watchdog
        live_usage = self.usage
        if watchdog.max_turns or watchdog.max_cost_usd:
            live_usage = await asyncio.to_thread(self.runtime.sample_usage, self.task)
        reason = watchdog.breach_reason(self.task, elapsed_seconds=time.monotonic() - self.started_at, usage=live_usage)
        if not reason:
            return False
        logger.warning("Watchdog interrupting stuck task %s: %s", self.task.pk, reason)
        return await self._interrupt(reason)


async def _drive_bound_session(
    task: Task,
    prompt: str,
    options: ClaudeAgentOptions,
    harness: Harness,
    *,
    runtime: HeartbeatRuntime,
) -> HarnessOutcome:
    usage = await asyncio.to_thread(runtime.sample_usage, task)
    started_at = time.monotonic()
    capture = StreamCapture(round_ceiling=_arm_round_ceiling(options, harness))

    async with harness.open(options) as session:
        beat = _Heartbeat(
            task=task, runtime=runtime, session=session, capture=capture, usage=usage, started_at=started_at
        )
        heartbeat_task = asyncio.create_task(beat.run())
        try:
            timeout = runtime.watchdog.max_runtime_seconds or None
            outcome = await asyncio.wait_for(_collect(session, prompt, capture), timeout=timeout)
        except TimeoutError:
            await session.interrupt()
            elapsed = time.monotonic() - started_at
            reason = runtime.watchdog.breach_reason(task, elapsed_seconds=elapsed, usage=usage) or (
                f"runtime ceiling exceeded: ran {elapsed:.0f}s without exiting"
            )
            outcome = capture.outcome(stuck_reason=reason)
        finally:
            heartbeat_task.cancel()

    return beat.folded(outcome)
