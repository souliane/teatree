"""Keep a finished, capable harness session open for bounded follow-up turns."""

import asyncio
import logging
from dataclasses import dataclass
from time import monotonic

from teatree.agents.harness import HarnessSession
from teatree.agents.live_control import MAX_FOLLOW_UP_TURNS, FollowUpSession, LiveSessionController
from teatree.agents.runner_stream import HarnessOutcome, StreamCapture, drain_turn

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class IdleWindowBudget:
    seconds: float
    deadline: float | None


def can_idle(session: HarnessSession, outcome: HarnessOutcome, live: LiveSessionController) -> bool:
    result = outcome.result_message
    return (
        isinstance(session, FollowUpSession)
        and live.state != "closed"
        and outcome.stuck_reason is None
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
            starting = asyncio.create_task(session.follow_up(follow_up.prompt))
            try:
                done, _ = await asyncio.wait({starting}, timeout=follow_up.wait)
            finally:
                if not starting.done():
                    starting.cancel()
            error = starting.exception() if done and not starting.cancelled() else TimeoutError()
            if error is not None:
                follow_up.started.set_result(error if isinstance(error, Exception) else RuntimeError("start cancelled"))
                live.close()
                break
            follow_up.started.set_result(None)
            try:
                outcome = await drain_turn(session, capture)
            except Exception:
                logger.warning("Task %s: follow-up turn failed", live.task.pk, exc_info=True)
                break
        return outcome
    finally:
        live.close()
