"""Lease heartbeat and watchdog driving for one open harness session."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.compaction_guard import GuardedHarness
from teatree.agents.harness import Harness
from teatree.agents.live_binding import bound_task_mailbox
from teatree.agents.live_control import LiveFence, LiveSessionController, LiveTask
from teatree.agents.live_idle import IdleWindowBudget, hold_idle_window
from teatree.agents.live_registry import shared_registry
from teatree.agents.round_ceiling import RoundCeiling
from teatree.agents.runner_stream import HarnessOutcome, StreamCapture, _collect
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.managers_task_claim import drain_block_reason
from teatree.core.models import LeaseLostError, Task
from teatree.core.models.task_claim import describe_lease_loss
from teatree.core.worktree.occupancy import WorktreeOccupancyLostError, renew_ticket_checkout, task_holder_id
from teatree.generation import current_generation
from teatree.utils.thread_db import close_thread_db_connections

logger = logging.getLogger(__name__)

UsageSampler = Callable[[Task], TaskUsage]
LeaseRenewer = Callable[[Task], None]

#: Beats a drain waits on an open tool call, so a push or merge is not cut in half yet the wait fits the drain grace.
CHECKPOINT_MAX_DEFER_BEATS = 3
HEARTBEAT_INTERVAL_SECONDS = 60
CHECKPOINT_GRACE_SECONDS = HEARTBEAT_INTERVAL_SECONDS * (CHECKPOINT_MAX_DEFER_BEATS + 2)


@dataclass(frozen=True, slots=True)
class HeartbeatRuntime:
    watchdog: LoopWatchdog
    heartbeat_interval: float
    sample_usage: UsageSampler
    renew_lease: LeaseRenewer
    drain_reason: Callable[[], str]
    idle_window_seconds: float = 0.0


def renew_lease_closing_connection(task: Task, *, lease_seconds: int) -> None:
    """Renew task/worktree leases and close this worker thread's DB connection."""
    try:
        task.renew_lease(lease_seconds=lease_seconds)
        if task.ticket_id is not None:
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


def _live_task(task: Task, options: ClaudeAgentOptions, harness: Harness) -> LiveTask:
    minted = LiveFence.mint()
    return LiveTask(
        pk=task.pk,
        ticket=task.ticket_id,
        phase=task.phase,
        harness=harness.name if isinstance(harness, GuardedHarness) else type(harness).__name__,
        model=options.model or "",
        fence=LiveFence(minted.attempt, task.claimed_generation or current_generation()),
    )


@dataclass
class _Heartbeat:
    """One run's beat: renew its lease, then interrupt it on a lost lease, a deploy drain or a watchdog breach."""

    task: Task
    runtime: HeartbeatRuntime
    live: LiveSessionController
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
                    continue
                if self.live.idle:
                    await self._close_idle_on_drain()
                    continue
                await self._restate_directions()
                # An interrupted run holds its claim until it ends, so only the lease is renewed from here on.
                if self.breach or await self._checkpointed():
                    continue
                await self._breached()
        finally:
            await asyncio.to_thread(close_thread_db_connections)

    def folded(self, outcome: HarnessOutcome) -> HarnessOutcome:
        if self.live.paused:
            return replace(outcome, stuck_reason="operator pause", paused=True)
        if self.breach and outcome.stuck_reason is None:
            return replace(
                outcome, stuck_reason=self.breach[0], lease_lost=self.lease_lost, checkpointed=self.checkpointed
            )
        return outcome

    async def _interrupt(self, reason: str) -> bool:
        self.breach.append(reason)
        await self.live.interrupt()
        return True

    async def _lost_the_lease(self) -> bool:
        try:
            await asyncio.to_thread(self.runtime.renew_lease, self.task)
        except LeaseLostError as exc:
            # The flags name what FIRST interrupted the run; a lost claim re-interrupts every beat until the drive ends.
            if self.breach:
                await self.live.interrupt()
                return True
            self.lease_lost = True
            logger.warning("Task %s lease lost; interrupting run", self.task.pk)
            return await self._interrupt(str(exc))
        except Exception:
            logger.warning("Heartbeat failed for task %s", self.task.pk, exc_info=True)
        return False

    async def _restate_directions(self) -> None:
        try:
            await self.live.restate_after_compaction()
        except Exception:
            logger.warning("Task %s: directions could not be restated after compaction", self.task.pk, exc_info=True)

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
        logger.warning("Task %s checkpointing for a drain: %s", self.task.pk, drain)
        return await self._interrupt(f"checkpoint: {drain}")

    async def _close_idle_on_drain(self) -> None:
        try:
            drain = await asyncio.to_thread(self.runtime.drain_reason)
        except Exception:
            logger.warning("Drain check failed for task %s; the idle window continues", self.task.pk, exc_info=True)
            return
        if drain:
            self.live.close()

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


async def drive_with_heartbeat(
    task: Task,
    prompt: str,
    options: ClaudeAgentOptions,
    harness: Harness,
    *,
    runtime: HeartbeatRuntime,
) -> HarnessOutcome:
    """Drive one session while renewing ownership and enforcing watchdog ceilings."""
    usage = await asyncio.to_thread(runtime.sample_usage, task)
    started_at = time.monotonic()
    capture = StreamCapture(round_ceiling=_arm_round_ceiling(options, harness))

    registry = shared_registry()
    live_task = _live_task(task, options, harness)
    claim_lost: str | None = None

    def compaction_stopped() -> bool:
        return isinstance(harness, GuardedHarness) and harness.guard.stopped_run

    async def claim_live() -> bool:
        def check() -> bool:
            nonlocal claim_lost
            try:
                runtime.renew_lease(task)
            except LeaseLostError as exc:
                claim_lost = str(exc)
                return False
            try:
                holds = Task.objects.filter(pk=task.pk, claimed_generation=task.claimed_generation).exists()
                if not holds:
                    claim_lost = f"lease lost for task {task.pk}: worker generation moved on"
                return holds
            finally:
                close_thread_db_connections()

        return await asyncio.to_thread(check)

    with bound_task_mailbox(options, live_task):
        async with (
            registry.attach(live_task, capture, claim_live=claim_live if task.claimed_at else None) as live,
            harness.open(options) as session,
        ):
            live.bind(session)
            beat = _Heartbeat(
                task=task, runtime=runtime, live=live, capture=capture, usage=usage, started_at=started_at
            )
            heartbeat_task = asyncio.create_task(beat.run())
            try:
                timeout = runtime.watchdog.max_runtime_seconds or None
                outcome = await asyncio.wait_for(_collect(session, prompt, capture), timeout=timeout)
                if runtime.idle_window_seconds > 0 and not compaction_stopped():
                    deadline = started_at + timeout if timeout is not None else None
                    outcome = await hold_idle_window(
                        session,
                        capture,
                        live,
                        outcome,
                        budget=IdleWindowBudget(
                            runtime.idle_window_seconds,
                            deadline,
                            compaction_stopped,
                        ),
                    )
            except TimeoutError:
                await live.interrupt()
                elapsed = time.monotonic() - started_at
                reason = runtime.watchdog.breach_reason(task, elapsed_seconds=elapsed, usage=usage) or (
                    f"runtime ceiling exceeded: ran {elapsed:.0f}s without exiting"
                )
                outcome = capture.outcome(stuck_reason=reason)
            finally:
                heartbeat_task.cancel()

    if claim_lost is not None and outcome.stuck_reason is None:
        outcome = replace(outcome, stuck_reason=claim_lost, lease_lost=True)
    return beat.folded(outcome)
