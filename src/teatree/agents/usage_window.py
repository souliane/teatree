"""Park-not-fail + admission guard for an exhausted Claude usage window (Directive #3).

When a agent dispatch hits a usage-window limit (the ~5h rolling session limit or the
7-day weekly limit), the old behaviour folded it into a terminal FAILED attempt and the
headless plane went idle forever until a human poked it — the measured 7.8h loss. This
module is what replaced it, permanently armed:

- :func:`park_task_on_limit` records a :class:`~teatree.core.models.UsageWindowState` for
    the lane and PARKS the task (returns it to the queue with ``not_before`` at the window's
    re-arm instant) instead of failing it. The self-rescheduling
    ``teatree.loops.usage_window_recovery`` chain clears the window + releases the parked
    tasks at reset.
- :func:`maybe_park_for_active_window` is the admission guard: while an uncleared window
    covers a dispatch's lane, further LLM dispatches on that lane are parked the same way
    rather than burning attempts that will 429.

This module owns the ``LimitCause`` → horizon resolution (it imports ``teatree.llm``);
the domain model stays llm-free and only persists the resolved instant.
"""

import dataclasses
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from django.utils import timezone

from teatree.agents.credential_policy import (
    ALL_ACCOUNTS_EXHAUSTED_CAUSE,
    EXHAUSTION_CAUSES,
    resolve_credential_provider,
)
from teatree.config import AgentHarnessProvider, get_effective_settings
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import LIMIT_PARKED_PREFIX, ModeOverride, Task, TaskAttempt, UsageWindowState
from teatree.core.notify import NotifyKind, notify_user
from teatree.llm.anthropic_limits import LimitCause, LimitMatch, window_horizon

if TYPE_CHECKING:
    from teatree.agents.attempt_recorder import AttemptUsage

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True, slots=True)
class LimitSignal:
    """What the attempt that hit the limit knows about itself — evidence, not disposal.

    ``sdk_resets_at`` and ``usage`` are both read off the SAME outcome that hit the limit
    (#4164): one is the SDK's own hint about when the window re-arms, the other is what that
    same result billed. ``account`` is the ``pass`` entry that dispatch signed with, resolved
    BEFORE the turn ran — the shared sticky pointer may name a different account by the time
    the limit is handled, so re-reading it there attributes the refusal to a bystander.
    Bundled so a park-chain function's signature does not grow every time a new fact about
    the triggering attempt needs threading through. The all-default ``LimitSignal()`` is the
    genuinely pre-dispatch case — nothing observed yet.
    """

    sdk_resets_at: int | None = None
    usage: "AttemptUsage | None" = None
    account: str = ""


#: The genuinely pre-dispatch case — nothing observed yet. A module-level singleton rather
#: than ``LimitSignal()`` inline in each signature (both fields are immutable, so sharing
#: this one instance is safe; ruff B008 bans a call directly in an argument default).
_NO_SIGNAL = LimitSignal()

#: The subscription causes a mid-run limit can ROTATE off (an account hit its 5h/weekly
#: window while others may still be healthy). A transient rate limit is lane-wide and
#: API-credit exhaustion has no rotation, so both stay on the plain lane park.
_ROTATABLE_SUBSCRIPTION_CAUSES: frozenset[LimitCause] = frozenset(
    {LimitCause.SUBSCRIPTION_SESSION, LimitCause.SUBSCRIPTION_WEEKLY},
)
#: How far ahead an ALREADY-ELAPSED reset is clamped when parking. A park keyed on a past
#: instant is dead on arrival — ``usage_window_recovery`` clears it on its very next tick, so
#: a caller that keeps re-deriving an elapsed reset re-parks at the poll cadence. A stale
#: reset means the true re-arm instant is UNKNOWN (an outage artefact, a clock skew, an SDK
#: processing delay), not that the lane is free, so the park is kept — just pushed far enough
#: ahead to be a real quiesce and re-checked soon after.
ELAPSED_RESET_GRACE = timedelta(minutes=5)


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


def _park_instant(task: Task, *, lane: str, cause: str, reset: datetime, moment: datetime) -> datetime:
    """*moment* when the next dispatch would ride the meter instead, else *reset*.

    Under plans-first, a spent plan is a rotation rather than a quiesce: the credential
    KIND changes exactly the way an account rotation changes the account, so the task goes
    back on the queue immediately and the very next tick re-dispatches it metered — the
    window row this reads was written a moment ago, so the policy already answers
    ``api_key``. Mid-run failover is impossible (a limit arrives on the terminal
    ``ResultMessage``, when the session is already over), which is why this is a requeue and
    not a swap. Every other shape — plans-only, a transient throttle, no usable metered
    credential — keeps today's park at the window's own re-arm instant.
    """
    if lane != TaskAttempt.Lane.SUBSCRIPTION or cause not in EXHAUSTION_CAUSES:
        return reset
    scope = task.ticket.overlay or ""
    configured = get_effective_settings(scope or None).agent_harness_provider
    failed_over = resolve_credential_provider(configured, scope=scope, now=moment)
    return moment if failed_over is AgentHarnessProvider.API_KEY else reset


def _epoch_to_datetime(epoch: int | None) -> datetime | None:
    """Convert the SDK's ``RateLimitInfo.resets_at`` Unix timestamp to an aware datetime."""
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _reported_reset(match: LimitMatch, signal: LimitSignal) -> datetime | None:
    """The reset the triggering result reported — the SDK's typed instant, else one its error text stated."""
    return _epoch_to_datetime(signal.sdk_resets_at) or match.stated_reset


def park_task_on_limit(
    task: Task,
    match: LimitMatch,
    *,
    lane: str,
    now: datetime | None = None,
    signal: LimitSignal = _NO_SIGNAL,
) -> TaskAttempt | None:
    """Park *task* behind the exhausted window instead of failing it — or ``None``.

    Returns ``None`` (so the caller records a terminal FAILED) when the cause has no
    time-based recovery (API-credit exhaustion). When it parks, it records the lane's
    :class:`UsageWindowState`, records a distinct ``limit_parked:`` :class:`TaskAttempt`,
    and returns the task to the queue PENDING with
    ``not_before`` at the window's re-arm instant. ``signal.usage`` carries the spend the SDK
    reported on the SAME result that triggered this park — pass it whenever a turn ran
    (#4164); leave the default ``LimitSignal()`` only when this park is genuinely pre-dispatch
    (nothing billed yet).
    """
    moment = now or timezone.now()
    reset = effective_resets_at(match.cause, _reported_reset(match, signal), moment)
    if reset is None:
        return None
    # The SDK's own ``resets_at`` can arrive already elapsed (processing delay, clock skew),
    # which would write a window the recovery chain clears immediately — same self-clearing
    # class the all-exhausted path guards. Clamp here too so BOTH writers are covered.
    reset = _future_park_instant(reset, moment)
    window = UsageWindowState.record_limit(lane=lane, cause=match.cause.value, resets_at=reset, now=moment)
    # #3159 item 6: auto-engage the low-token preset for the parked window's tenure
    # (default-off flag; never overwrites a live user override). Fail-soft — a park
    # must never depend on the preset layer.
    _auto_engage_token_outage(reset)
    if match.cause is LimitCause.PROVIDER_BUDGET:
        _alert_owner_of_budget_park(window, match, reset=reset)
    not_before = _park_instant(task, lane=lane, cause=match.cause.value, reset=reset, moment=moment)
    logger.warning(
        "Task %s parked behind an exhausted usage window (%s, lane=%r) until %s",
        task.pk,
        match.cause.value,
        lane or "ambient",
        not_before.isoformat(),
    )
    return _record_park(
        task, reason=f"{LIMIT_PARKED_PREFIX}{match.as_reason()}", not_before=not_before, usage=signal.usage
    )


def park_or_rotate_on_limit(
    task: Task,
    match: LimitMatch,
    *,
    lane: str,
    now: datetime | None = None,
    signal: LimitSignal = _NO_SIGNAL,
) -> TaskAttempt | None:
    """Reactive limit handler: rotate accounts before parking (multi-account #C1), or park.

    On a subscription session/weekly limit, record the CURRENT account exhausted and
    re-consult the credential selector (routing scope = the task's ticket overlay): if another
    account is still healthy, REQUEUE the task to rotate onto it (no lane park) so the next
    dispatch runs on the fresh account; only when every account is exhausted does the whole
    lane park (auto-resume at the earliest reset). A single unrouted credential, a transient
    rate limit, or an API-credit cause falls through to :func:`park_task_on_limit` unchanged.
    ``None`` (→ caller records a terminal FAILED) when the cause has no time-based
    recovery; a leak block also alerts the owner once, since nothing will ever retry it.
    ``signal.usage`` is the spend the triggering result billed — this handler
    only ever fires POST-turn (a limit was reported ON a result), so it is carried through
    to whichever :class:`TaskAttempt` records the park (#4164).
    """
    if match.cause is LimitCause.LEAK_BLOCKED:
        _alert_owner_of_leak_block(task, match)
        return None
    moment = now or timezone.now()
    reset = effective_resets_at(match.cause, _reported_reset(match, signal), moment)
    if reset is None:
        return None
    if lane == TaskAttempt.Lane.SUBSCRIPTION and match.cause in _ROTATABLE_SUBSCRIPTION_CAUSES:
        rotated = _rotate_or_none(task, match, reset=reset, moment=moment, signal=signal)
        if rotated is not None:
            return rotated
    return park_task_on_limit(task, match, lane=lane, now=moment, signal=signal)


def _rotate_or_none(
    task: Task,
    match: LimitMatch,
    *,
    reset: datetime,
    moment: datetime,
    signal: LimitSignal = _NO_SIGNAL,
) -> TaskAttempt | None:
    """Record the exhausted account + reselect: requeue to rotate, park if all spent, else ``None``.

    ``None`` means nothing was routed (no account named and no sticky pick), so the caller
    falls back to the plain lane park. ``signal.account`` is the one this dispatch signed
    with; an empty one leaves the sticky pointer to decide, as for a harness that resolves
    its credential lazily. The credential import is call-time so the domain credential factory
    is only pulled in when a subscription limit actually fires. The routing scope is always
    the task's own ticket overlay — derived here rather than pre-computed by the caller, so it
    is not a second parameter naming the same fact.
    """
    from teatree.credential_config import (  # noqa: PLC0415 — call-time import (domain credential factory)
        AllTokensExhaustedError,
        ReactiveLimit,
        record_reactive_exhaustion_and_reselect,
    )

    scope = task.ticket.overlay or ""  # the overlay the per-account selector routes for
    limit = ReactiveLimit(
        resets_at=reset,
        weekly=match.cause is LimitCause.SUBSCRIPTION_WEEKLY,
        pass_path=signal.account or None,
    )
    try:
        healthy = record_reactive_exhaustion_and_reselect(scope=scope, limit=limit, now=moment)
    except AllTokensExhaustedError as exc:
        return park_task_on_all_exhausted(
            task,
            resets_at=exc.earliest_reset or reset,
            lane=TaskAttempt.Lane.SUBSCRIPTION,
            now=moment,
            usage=signal.usage,
        )
    if healthy is None:
        return None
    logger.info("Task %s rotating off an exhausted subscription account to a healthy one", task.pk)
    return _requeue_for_rotation(task, moment=moment, usage=signal.usage)


def _requeue_for_rotation(task: Task, *, moment: datetime, usage: "AttemptUsage | None" = None) -> TaskAttempt:
    """Return *task* to the queue immediately (PENDING) so the next dispatch rotates accounts.

    Records the same ``limit_parked:`` audit attempt shape :func:`_record_park` uses (excluded
    from the repair budget — a rotation is a scheduling event, not a work iteration), then
    parks with ``not_before`` at *moment* so the task is claimable on the next tick.
    """
    reason = f"{LIMIT_PARKED_PREFIX}rotating to a healthy subscription account (an account hit its window)"
    return _record_park(task, reason=reason, not_before=moment, usage=usage)


def park_task_on_all_exhausted(
    task: Task,
    *,
    resets_at: datetime | None,
    lane: str,
    now: datetime | None = None,
    usage: "AttemptUsage | None" = None,
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

    ``usage`` is ``None`` at this function's own pre-dispatch caller (a ``CredentialError``
    raised before any turn ran) but carries real spend when reached via the post-turn rotation
    path (:func:`_rotate_or_none`), so it is a caller-supplied parameter, never assumed either way.
    """
    if resets_at is None:
        return None
    moment = now or timezone.now()
    reset = _future_park_instant(resets_at, moment)
    UsageWindowState.record_limit(lane=lane, cause=ALL_ACCOUNTS_EXHAUSTED_CAUSE, resets_at=reset, now=moment)
    _auto_engage_token_outage(reset)
    not_before = _park_instant(task, lane=lane, cause=ALL_ACCOUNTS_EXHAUSTED_CAUSE, reset=reset, moment=moment)
    logger.warning(
        "Task %s parked — all %s accounts exhausted; auto-resume at %s",
        task.pk,
        lane or "ambient",
        not_before.isoformat(),
    )
    reason = f"{LIMIT_PARKED_PREFIX}all configured subscription accounts exhausted — auto-resume at reset"
    return _record_park(task, reason=reason, not_before=not_before, usage=usage)


def _auto_engage_token_outage(reset: datetime) -> None:
    try:
        ModeOverride.objects.auto_engage_token_outage(resets_at=reset)
    except Exception:
        logger.warning("token-outage auto-engage failed on park — continuing", exc_info=True)


def _alert_owner_of_leak_block(task: Task, match: LimitMatch) -> None:
    """Tell the owner once per task: the task stays FAILED until what leaked into its context is found."""
    text = (
        f"Leak block on {task.display_subject()} (phase `{task.phase}`): {match.phrase}. The request was refused "
        "before the model saw it; the task is FAILED and will not be retried until the content is removed."
    )
    try:
        notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key=f"leak_blocked:{task.pk}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.warning("owner alert for a leak block failed — continuing", exc_info=True)


def _alert_owner_of_budget_park(window: UsageWindowState, match: LimitMatch, *, reset: datetime) -> None:
    """Tell the owner once per window: only raising the cap or funding the wallet resumes the lane sooner."""
    text = (
        f"Metered lane parked: the provider refused on a spend limit ({match.phrase}). "
        f"Dispatches on lane `{window.lane or 'ambient'}` resume at {reset.astimezone(UTC):%Y-%m-%d %H:%M} UTC; "
        "raise the key's spend cap or fund the wallet to resume sooner."
    )
    try:
        notify_user(
            text,
            kind=NotifyKind.INFO,
            idempotency_key=f"provider_budget_parked:{window.pk}",
            audience=NotifyAudience.OWNER_ESCALATION,
        )
    except Exception:
        logger.warning("owner alert for a provider-budget park failed — continuing", exc_info=True)


def maybe_park_for_active_window(task: Task, *, lane: str, now: datetime | None = None) -> TaskAttempt | None:
    """Admission guard — park *task* if an uncleared window still covers *lane*, else ``None``.

    ``None`` (dispatch proceeds) when no window covers the lane, or the covering window's
    reset has already passed (recovery will clear it, so let the dispatch try). Otherwise
    the task is parked with ``not_before`` at the window's re-arm instant — the same shape
    :func:`park_task_on_limit` produces — so no attempt is burned on a lane
    that will 429.
    """
    moment = now or timezone.now()
    window = UsageWindowState.objects.active_for_lane(lane)
    if window is None or window.resets_at is None or window.should_clear(moment):
        return None
    reason = f"{LIMIT_PARKED_PREFIX}admission: {window.cause} window on lane {lane or 'ambient'!r} active"
    return _record_park(task, reason=reason, not_before=window.resets_at)


def _record_park(task: Task, *, reason: str, not_before: datetime, usage: "AttemptUsage | None" = None) -> TaskAttempt:
    """Record the parked ``TaskAttempt`` and return the task to the queue (never fail it).

    The park sibling of ``headless._record_failure``: it creates the audit attempt with the
    ``limit_parked:`` marker (excluded from the repair-loop budget) then calls
    :meth:`Task.park` — the task ends PENDING with a future ``not_before``, never FAILED.

    ``usage`` is ``None`` only for a genuinely PRE-dispatch park (the admission guard, a
    pre-turn credential gap) — those callers pass nothing and the spend columns stay NULL. A
    REACTIVE park (a limit reported ON a result the SDK already returned) is POST-turn like
    every other outcome branch and passes its sampled ``usage`` through (#4164) — a park is
    not exempt from that rule, only genuinely pre-dispatch callers are.
    """
    from teatree.agents.attempt_recorder import (  # noqa: PLC0415 — deferred: call-time import
        usage_fields,
        with_transport_records,
    )

    attempt = TaskAttempt.objects.create(
        task=task,
        ended_at=timezone.now(),
        exit_code=1,
        error=reason,
        result=with_transport_records(None, usage),
        **usage_fields(usage),
    )
    task.park(not_before=not_before)
    return attempt
