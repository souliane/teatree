"""Lease heartbeat and watchdog driving for one open harness session."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.harness import Harness
from teatree.agents.live_mailbox import bound_task_mailbox
from teatree.agents.runner_stream import HarnessOutcome, StreamCapture, _collect
from teatree.agents.runner_watchdog import LoopWatchdog, TaskUsage
from teatree.core.models import LeaseLostError, Task
from teatree.core.models.task_claim import describe_lease_loss
from teatree.core.worktree.occupancy import WorktreeOccupancyLostError, renew_ticket_checkout, task_holder_id
from teatree.utils.thread_db import close_thread_db_connections

logger = logging.getLogger(__name__)

UsageSampler = Callable[[Task], TaskUsage]
LeaseRenewer = Callable[[Task], None]


@dataclass(frozen=True, slots=True)
class HeartbeatRuntime:
    watchdog: LoopWatchdog
    heartbeat_interval: float
    sample_usage: UsageSampler
    renew_lease: LeaseRenewer


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
    breach: list[str] = []
    lease_lost = False
    capture = StreamCapture()

    async with harness.open(options) as session:

        async def heartbeat() -> None:
            nonlocal lease_lost
            try:
                while True:
                    await asyncio.sleep(runtime.heartbeat_interval)
                    try:
                        await asyncio.to_thread(runtime.renew_lease, task)
                    except LeaseLostError as exc:
                        breach.append(str(exc))
                        lease_lost = True
                        logger.warning("Task %s lease lost; interrupting duplicate run", task.pk)
                        await session.interrupt()
                        return
                    except Exception:
                        logger.warning("Heartbeat failed for task %s", task.pk, exc_info=True)
                    live_usage = usage
                    if runtime.watchdog.max_turns or runtime.watchdog.max_cost_usd:
                        live_usage = await asyncio.to_thread(runtime.sample_usage, task)
                    reason = runtime.watchdog.breach_reason(
                        task,
                        elapsed_seconds=time.monotonic() - started_at,
                        usage=live_usage,
                    )
                    if reason and not breach:
                        breach.append(reason)
                        logger.warning("Watchdog interrupting stuck task %s: %s", task.pk, reason)
                        await session.interrupt()
                        return
            finally:
                await asyncio.to_thread(close_thread_db_connections)

        heartbeat_task = asyncio.create_task(heartbeat())
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

    if breach and outcome.stuck_reason is None:
        outcome = replace(outcome, stuck_reason=breach[0], lease_lost=lease_lost)
    return outcome
