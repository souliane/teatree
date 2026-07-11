"""Park-not-fail + admission guard for an exhausted Claude usage window (Directive #3).

When a headless dispatch hits a usage-window limit (the ~5h rolling session limit or the
7-day weekly limit), the old behaviour folded it into a terminal FAILED attempt and the
headless plane went idle forever until a human poked it — the measured 7.8h loss. This
module is the DARK, opt-in alternative gated by ``limit_autorecovery_enabled``:

- :func:`park_task_on_limit` records a :class:`~teatree.core.models.UsageWindowState` for
    the lane and PARKS the task (returns it to the queue with ``not_before`` at the window's
    re-arm instant) instead of failing it. The self-rescheduling
    ``teatree.loops.usage_window_recovery`` chain clears the window + releases the parked
    tasks at reset.
- :func:`maybe_park_for_active_window` is the admission guard: while an uncleared window
    covers a dispatch's lane, further LLM dispatches on that lane are parked the same way
    rather than burning attempts that will 429.

Both are no-ops when the flag is OFF (the default), so the flag-off path is byte-identical
to today. This module owns the ``LimitCause`` → horizon resolution (it imports
``teatree.llm``); the domain model stays llm-free and only persists the resolved instant.
"""

import logging
from datetime import UTC, datetime

from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.models import LIMIT_PARKED_PREFIX, LoopPresetOverride, Task, TaskAttempt, UsageWindowState
from teatree.llm.anthropic_limits import LimitCause, LimitMatch, window_horizon

logger = logging.getLogger(__name__)


def autorecovery_enabled() -> bool:
    """Whether ``limit_autorecovery_enabled`` resolves ON — fail-safe OFF.

    A read failure degrades to OFF: the whole feature is opt-in, so an unreadable flag must
    never silently change dispatch behaviour (the flag-off path is byte-identical to today).
    """
    try:
        return bool(get_effective_settings().limit_autorecovery_enabled)
    except Exception:
        logger.debug("limit_autorecovery_enabled read failed — treating auto-recovery as OFF", exc_info=True)
        return False


def effective_resets_at(cause: LimitCause, sdk_resets_at: datetime | None, now: datetime) -> datetime | None:
    """The instant the window re-arms — the SDK's ``resets_at`` when present, else the horizon.

    ``None`` when the cause has no time-based recovery (API-credit exhaustion): there is
    nothing to re-arm to, so the caller does NOT park (it records a terminal FAILED as
    today — the operator must add credits). The cause is checked FIRST: an ``overage``
    rejection maps to :class:`LimitCause.API_CREDIT` yet can still carry a top-level
    ``resets_at`` on the SDK event, so trusting that value before the cause would park a
    credit-exhausted lane and spin it (park → recover → re-dispatch → re-park). A
    horizonless cause is terminal even when the SDK reports a reset.
    """
    horizon = window_horizon(cause)
    if horizon is None:
        return None
    return sdk_resets_at if sdk_resets_at is not None else now + horizon


def _epoch_to_datetime(epoch: int | None) -> datetime | None:
    """Convert the SDK's ``RateLimitInfo.resets_at`` Unix timestamp to an aware datetime."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def park_task_on_limit(
    task: Task,
    match: LimitMatch,
    *,
    sdk_resets_at: int | None,
    lane: str,
    now: datetime | None = None,
) -> TaskAttempt | None:
    """Park *task* behind the exhausted window instead of failing it — or ``None``.

    Returns ``None`` (so the caller records a terminal FAILED, byte-identical to today) when
    the flag is OFF or the cause has no time-based recovery (API-credit exhaustion). When it
    parks, it records the lane's :class:`UsageWindowState`, records a distinct
    ``limit_parked:`` :class:`TaskAttempt`, and returns the task to the queue PENDING with
    ``not_before`` at the window's re-arm instant.
    """
    if not autorecovery_enabled():
        return None
    moment = now or timezone.now()
    reset = effective_resets_at(match.cause, _epoch_to_datetime(sdk_resets_at), moment)
    if reset is None:
        return None
    UsageWindowState.record_limit(lane=lane, cause=match.cause.value, resets_at=reset, now=moment)
    # #3159 item 6: auto-engage the low-power preset for the parked window's tenure
    # (default-off flag; never overwrites a live user override). Fail-soft — a park
    # must never depend on the preset layer.
    _auto_engage_low_power(reset, moment)
    logger.warning(
        "Task %s parked behind an exhausted usage window (%s, lane=%r) until %s",
        task.pk,
        match.cause.value,
        lane or "ambient",
        reset.isoformat(),
    )
    return _record_park(task, reason=f"{LIMIT_PARKED_PREFIX}{match.as_reason()}", not_before=reset)


def _auto_engage_low_power(reset: datetime, moment: datetime) -> None:
    try:
        LoopPresetOverride.objects.auto_engage_low_power(resets_at=reset, now=moment)
    except Exception:
        logger.warning("low-power auto-engage failed on park — continuing", exc_info=True)


def maybe_park_for_active_window(task: Task, *, lane: str, now: datetime | None = None) -> TaskAttempt | None:
    """Admission guard — park *task* if an uncleared window still covers *lane*, else ``None``.

    ``None`` (dispatch proceeds) when the flag is OFF, no window covers the lane, or the
    covering window's reset has already passed (recovery will clear it, so let the dispatch
    try). Otherwise the task is parked with ``not_before`` at the window's re-arm instant —
    the same shape :func:`park_task_on_limit` produces — so no attempt is burned on a lane
    that will 429.
    """
    if not autorecovery_enabled():
        return None
    moment = now or timezone.now()
    window = UsageWindowState.objects.active_for_lane(lane)
    if window is None or window.resets_at is None or window.should_clear(moment):
        return None
    reason = f"{LIMIT_PARKED_PREFIX}admission: {window.cause} window on lane {lane or 'ambient'!r} active"
    return _record_park(task, reason=reason, not_before=window.resets_at)


def _record_park(task: Task, *, reason: str, not_before: datetime) -> TaskAttempt:
    """Record the parked ``TaskAttempt`` and return the task to the queue (never fail it).

    The park sibling of ``headless._record_failure``: it creates the audit attempt with the
    ``limit_parked:`` marker (excluded from the repair-loop budget) then calls
    :meth:`Task.park` — the task ends PENDING with a future ``not_before``, never FAILED.
    """
    attempt = TaskAttempt.objects.create(
        task=task,
        execution_target=task.execution_target,
        ended_at=timezone.now(),
        exit_code=1,
        error=reason,
    )
    task.park(not_before=not_before)
    return attempt
