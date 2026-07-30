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
from datetime import UTC, datetime, timedelta

from django.utils import timezone

from teatree.config import get_effective_settings
from teatree.core.models import LIMIT_PARKED_PREFIX, ModeOverride, Task, TaskAttempt, UsageWindowState
from teatree.llm.anthropic_limits import LimitCause, LimitMatch, window_horizon

logger = logging.getLogger(__name__)

#: The subscription causes a mid-run limit can ROTATE off (an account hit its 5h/weekly
#: window while others may still be healthy). A transient rate limit is lane-wide and
#: API-credit exhaustion has no rotation, so both stay on the plain lane park.
_ROTATABLE_SUBSCRIPTION_CAUSES: frozenset[LimitCause] = frozenset(
    {LimitCause.SUBSCRIPTION_SESSION, LimitCause.SUBSCRIPTION_WEEKLY},
)
#: The ``UsageWindowState.cause`` recorded when the WHOLE subscription lane parked because
#: every account drained — distinct from a single-account cause for audit / notify wording.
_ALL_EXHAUSTED_CAUSE = "all_accounts_exhausted"
#: How far ahead an ALREADY-ELAPSED reset is clamped when parking. A park keyed on a past
#: instant is dead on arrival — ``usage_window_recovery`` clears it on its very next tick, so
#: a caller that keeps re-deriving an elapsed reset re-parks at the poll cadence. A stale
#: reset means the true re-arm instant is UNKNOWN (an outage artefact, a clock skew, an SDK
#: processing delay), not that the lane is free, so the park is kept — just pushed far enough
#: ahead to be a real quiesce and re-checked soon after.
ELAPSED_RESET_GRACE = timedelta(minutes=5)


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


def _future_park_instant(reset: datetime, moment: datetime) -> datetime:
    """*reset* if it is still ahead, else *moment* + :data:`ELAPSED_RESET_GRACE`.

    The one clamp both park writers apply, so neither can persist a
    :class:`~teatree.core.models.UsageWindowState` that the recovery chain clears on its very
    next tick (a self-clearing window notifies the owner and re-parks at the poll cadence).
    An elapsed reset is not evidence the lane is usable — only that the recorded instant is
    stale — so the window is kept and re-checked after the grace, never dropped.
    """
    return reset if reset > moment else moment + ELAPSED_RESET_GRACE


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
    # The SDK's own ``resets_at`` can arrive already elapsed (processing delay, clock skew),
    # which would write a window the recovery chain clears immediately — same self-clearing
    # class the all-exhausted path guards. Clamp here too so BOTH writers are covered.
    reset = _future_park_instant(reset, moment)
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


def park_or_rotate_on_limit(
    task: Task, match: LimitMatch, *, sdk_resets_at: int | None, lane: str, now: datetime | None = None
) -> TaskAttempt | None:
    """Reactive limit handler: rotate accounts before parking (multi-account #C1), or park.

    On a subscription session/weekly limit, record the CURRENT account exhausted and
    re-consult the credential selector (routing scope = the task's ticket overlay): if another
    account is still healthy, REQUEUE the task to rotate onto it (no lane park) so the next
    dispatch runs on the fresh account; only when every account is exhausted does the whole
    lane park (auto-resume at the earliest reset). A single unrouted credential, a transient
    rate limit, or an API-credit cause falls through to :func:`park_task_on_limit` unchanged.
    ``None`` (→ caller records a terminal FAILED, byte-identical to today) when the flag is OFF
    or the cause has no time-based recovery.
    """
    if not autorecovery_enabled():
        return None
    moment = now or timezone.now()
    reset = effective_resets_at(match.cause, _epoch_to_datetime(sdk_resets_at), moment)
    if reset is None:
        return None
    if lane == TaskAttempt.Lane.SUBSCRIPTION and match.cause in _ROTATABLE_SUBSCRIPTION_CAUSES:
        scope = task.ticket.overlay or ""  # the overlay the per-account selector routes for
        rotated = _rotate_or_none(task, match, reset=reset, scope=scope, moment=moment)
        if rotated is not None:
            return rotated
    return park_task_on_limit(task, match, sdk_resets_at=sdk_resets_at, lane=lane, now=moment)


def _rotate_or_none(
    task: Task, match: LimitMatch, *, reset: datetime, scope: str, moment: datetime
) -> TaskAttempt | None:
    """Record the current account exhausted + reselect: requeue to rotate, park if all spent, else ``None``.

    ``None`` means nothing was routed (no sticky account), so the caller falls back to the
    plain lane park. The credential import is call-time so the domain credential factory is
    only pulled in when a subscription limit actually fires.
    """
    from teatree.credential_config import (  # noqa: PLC0415 — call-time import (domain credential factory)
        AllTokensExhaustedError,
        record_reactive_exhaustion_and_reselect,
    )

    weekly = match.cause is LimitCause.SUBSCRIPTION_WEEKLY
    try:
        healthy = record_reactive_exhaustion_and_reselect(scope=scope, resets_at=reset, weekly=weekly, now=moment)
    except AllTokensExhaustedError as exc:
        return park_task_on_all_exhausted(
            task, resets_at=exc.earliest_reset or reset, lane=TaskAttempt.Lane.SUBSCRIPTION, now=moment
        )
    if healthy is None:
        return None
    logger.info("Task %s rotating off an exhausted subscription account to a healthy one", task.pk)
    return _requeue_for_rotation(task, moment=moment)


def _requeue_for_rotation(task: Task, *, moment: datetime) -> TaskAttempt:
    """Return *task* to the queue immediately (PENDING) so the next dispatch rotates accounts.

    Records the same ``limit_parked:`` audit attempt shape :func:`_record_park` uses (excluded
    from the repair budget — a rotation is a scheduling event, not a work iteration), then
    parks with ``not_before`` at *moment* so the task is claimable on the next tick.
    """
    reason = f"{LIMIT_PARKED_PREFIX}rotating to a healthy subscription account (an account hit its window)"
    return _record_park(task, reason=reason, not_before=moment)


def park_task_on_all_exhausted(
    task: Task, *, resets_at: datetime | None, lane: str, now: datetime | None = None
) -> TaskAttempt | None:
    """Park *task* behind an ALL-ACCOUNTS-exhausted lane (multi-account #C2) — or ``None``.

    Every configured account is spent, so there is no account to rotate to: park the WHOLE
    lane keyed on *resets_at* (the soonest instant ANY account frees up — see
    ``AnthropicTokenUsage.frees_up_at``) so the existing
    ``usage_window_recovery`` chain auto-resumes the task when the soonest account frees up — a
    quiesce, NOT a human escalation. ``None`` (caller records a terminal FAILED, as today) only
    when the flag is OFF or no reset is known at all (nothing to re-arm to).

    An ALREADY-ELAPSED *resets_at* is clamped to :data:`ELAPSED_RESET_GRACE` ahead rather than
    refused. Parking on a past instant would be dead on arrival — the recovery chain clears it
    on its very next tick — but REFUSING outright is worse than the churn it prevents: the
    caller then records a terminal FAILED whose ``all tokens exhausted`` signature maps to
    :attr:`LimitCause.SUBSCRIPTION_WEEKLY`, so the transient-requeue horizon is SEVEN DAYS. A
    stale reset is normally an outage artefact that clears in minutes, so the grace clamp keeps
    recovery at minutes scale while still keying the window in the FUTURE (no instant
    self-clear).
    """
    if not autorecovery_enabled() or resets_at is None:
        return None
    moment = now or timezone.now()
    reset = _future_park_instant(resets_at, moment)
    UsageWindowState.record_limit(lane=lane, cause=_ALL_EXHAUSTED_CAUSE, resets_at=reset, now=moment)
    _auto_engage_low_power(reset, moment)
    logger.warning(
        "Task %s parked — all %s accounts exhausted; auto-resume at %s",
        task.pk,
        lane or "ambient",
        reset.isoformat(),
    )
    reason = f"{LIMIT_PARKED_PREFIX}all configured subscription accounts exhausted — auto-resume at reset"
    return _record_park(task, reason=reason, not_before=reset)


def _auto_engage_low_power(reset: datetime, moment: datetime) -> None:
    try:
        ModeOverride.objects.auto_engage_low_power(resets_at=reset, now=moment)
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
