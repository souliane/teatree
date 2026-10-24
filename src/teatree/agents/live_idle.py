"""Keep a finished, capable harness session open for bounded follow-up turns."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from time import monotonic

from teatree.agents.harness import HarnessSession
from teatree.agents.live_control import MAX_FOLLOW_UP_TURNS, FollowUp, FollowUpSession, LiveSessionController
from teatree.agents.runner_stream import HarnessOutcome, StreamCapture, drain_turn
from teatree.core.models.task_attempt import RESUME_CONTEXT_CAP_TOKENS

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IdleWindowBudget:
    seconds: float
    deadline: float | None
    compaction_stopped: Callable[[], bool] | None = None


def can_idle(session: HarnessSession, outcome: HarnessOutcome, live: LiveSessionController) -> bool:
    result = outcome.result_message
    fits_resume_cap = outcome.context_tokens is None or outcome.context_tokens <= RESUME_CONTEXT_CAP_TOKENS
    if not fits_resume_cap:
        logger.info("Task %s: no idle window, conversation is %s tokens", live.task.pk, outcome.context_tokens)
    return (
        isinstance(session, FollowUpSession)
        and live.state != "closed"
        and fits_resume_cap
        and outcome.stuck_reason is None
        and not outcome.compaction_stopped
        and not outcome.cut_short
        and not outcome.round_handoff
        and result is not None
        and not result.is_error
    )


async def hold_idle_window(
    session: HarnessSession,
    capture: StreamCapture,
    live: LiveSessionController,
    outcome: HarnessOutcome,
    *,
    budget: IdleWindowBudget,
) -> HarnessOutcome:
    try:
        if not isinstance(session, FollowUpSession):
            return outcome
        for _ in range(MAX_FOLLOW_UP_TURNS):
            remaining = (
                budget.seconds if budget.deadline is None else min(budget.seconds, budget.deadline - monotonic())
            )
            if remaining <= 0 or not can_idle(session, outcome, live):
                break
            window = live.begin_idle()
            follow_up = await live.wait_idle(window, remaining)
            if follow_up is None:
                break
            if not await _start_follow_up(session, follow_up, live, budget):
                break
            previous_result = capture.result_message
            capture.result_message = None
            outcome = await _drain_follow_up(session, capture, live, budget)
            if outcome.result_message is None:
                capture.result_message = previous_result
                outcome = replace(outcome, result_message=previous_result)
            if outcome.stuck_reason is not None or outcome.compaction_stopped:
                break
        return outcome
    finally:
        live.close()


async def _start_follow_up(
    session: FollowUpSession, follow_up: FollowUp, live: LiveSessionController, budget: IdleWindowBudget
) -> bool:
    if live.state == "closed":
        return False
    starting = asyncio.create_task(session.follow_up(follow_up.prompt))
    try:
        start_wait = (
            follow_up.wait if budget.deadline is None else min(follow_up.wait, max(0, budget.deadline - monotonic()))
        )
        done, _ = await asyncio.wait({starting}, timeout=start_wait)
    finally:
        if not starting.done():
            starting.cancel()
    error = starting.exception() if done and not starting.cancelled() else TimeoutError()
    if error is not None:
        if not follow_up.started.done():
            follow_up.started.set_result(error if isinstance(error, Exception) else RuntimeError("start cancelled"))
        live.close()
        return False
    if follow_up.started.done():
        return False
    follow_up.started.set_result(None)
    return True


async def _drain_follow_up(
    session: HarnessSession, capture: StreamCapture, live: LiveSessionController, budget: IdleWindowBudget
) -> HarnessOutcome:
    try:
        drain_wait = None if budget.deadline is None else max(0, budget.deadline - monotonic())
        outcome = await asyncio.wait_for(drain_turn(session, capture), timeout=drain_wait)
    except TimeoutError:
        return capture.outcome(stuck_reason="runtime ceiling exceeded during follow-up turn")
    except Exception as exc:
        logger.warning("Task %s: follow-up turn failed", live.task.pk, exc_info=True)
        return capture.outcome(stuck_reason=f"follow-up turn failed: {type(exc).__name__}")
    if budget.compaction_stopped is not None and budget.compaction_stopped():
        return replace(outcome, compaction_stopped=True)
    if outcome.result_message is None:
        return replace(outcome, stuck_reason="follow-up turn ended without a result")
    return outcome
