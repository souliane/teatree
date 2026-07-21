"""The self-rescheduling mode-transition chain — side-effects only (#3159, #61).

Mode resolution is entirely read-time: a scheduled switch costs zero writes and
zero tokens (the mask simply resolves differently once the clock crosses a
boundary). This chain handles the *side-effects* of a switch, and nothing the
resolution itself depends on — so if the chain is down, modes still switch on
time; only the notification / drain lags (fail-soft). One self-rescheduling
``preset_transitions`` job on the existing ``LOOPS_QUEUE`` (the
``usage_window_recovery`` pattern, NO OS cron, ~0 tokens idle) that on each fire:

1. reaps a manual override whose ``until`` has passed (it is already inert at read
    time; this deletes the stale row);
2. detects "the resolved mode changed since the last stamp" and, on a change that
    RETURNS the box to reachable (the resolved ``defers_questions`` flips T→F, e.g.
    a scheduled ``offline``→``engaged`` boundary), fires the deferred-question drain.
    The availability-pin push is GONE (#61): the mode IS availability, so there is no
    separate pin to write — the intrinsic booleans already carry the posture;
3. posts ONE Slack line per switch.

The transition stamp is internal runtime state kept in a ``ConfigSetting`` row
(no extra migration): the last-applied mode name. Fail-soft throughout — any error
is logged and the chain re-schedules.
"""

import datetime as dt
import logging
from typing import Any

from django.tasks import task
from django.utils import timezone
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.core.mode_resolution import resolve_active_mode
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import ConfigSetting, LoopPreset, LoopPresetOverride
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.notify_question_drains import drain_deferred_questions
from teatree.loop.preset_resolution import ActivePreset, resolve_active_preset
from teatree.loops.timer_chains import LOOPS_QUEUE

logger = logging.getLogger(__name__)

TRANSITION_POLL_SECONDS = 60

_STAMP_KEY = "loop_preset_transition_stamp"


def apply_preset_transition(now: dt.datetime) -> dict[str, Any]:
    """Run one transition pass: reap expired override, drain + Slack line on a switch.

    Idempotent: with no change since the last stamp, only the expired-override reap
    runs and the pass is otherwise a no-op. Fail-soft — a side-effect failure is
    logged and never propagates (resolution is unaffected either way).
    """
    reaped = _reap_expired_overrides(now)
    active = resolve_active_preset(now)
    current_name = active.preset.name if active is not None else ""
    prior_name = _read_stamp(_STAMP_KEY)
    if current_name == prior_name:
        return {"reaped": reaped, "unchanged": 1}

    _drain_on_scheduled_return(prior_name)
    _post_switch_line(active, now)
    _write_stamp(_STAMP_KEY, current_name)
    return {"reaped": reaped, "switched": current_name}


def _reap_expired_overrides(now: dt.datetime) -> int:
    deleted, _ = LoopPresetOverride.objects.filter(until__isnull=False, until__lte=now).delete()
    return deleted


def _drain_on_scheduled_return(prior_name: str) -> None:
    """Fire the deferred-question drain when a scheduled switch returns to reachable.

    The merge folds availability into the mode, so a scheduled boundary crossing (an
    ``offline`` / ``unattended`` window ending) is the equivalent of the old
    away→present transition: when the PRIOR mode deferred questions and the newly
    resolved mode does not, the durable backlog drains to the user's Slack DM. The
    manual-override path drains in its own chokepoint; this covers schedule / default
    flips. Fail-open — a drain failure never blocks the transition.
    """
    prior = LoopPreset.objects.by_name(prior_name) if prior_name else None
    prior_defers = bool(prior.defers_questions) if prior is not None else False
    if not prior_defers or resolve_active_mode().defers_questions:
        return
    try:
        drain_deferred_questions()
    except Exception as exc:  # noqa: BLE001 — drain is best-effort; never block the transition
        logger.warning("scheduled return→reachable auto-drain failed: %s", exc)


def _post_switch_line(active: ActivePreset | None, now: dt.datetime) -> None:
    if active is None:
        text = "Loop preset cleared — loops resolve per base config again."
        key = f"loop_preset_switch:none:{now:%Y%m%d%H%M}"
    else:
        boundary = "" if active.until is None else f", until {timezone.localtime(active.until):%H:%M}"
        text = f"Loop preset → {active.preset.name} ({active.reason}{boundary})."
        key = f"loop_preset_switch:{active.preset.name}:{now:%Y%m%d%H%M}"
    try:
        notify_user(text, kind=NotifyKind.INFO, idempotency_key=key, audience=NotifyAudience.INTERNAL)
    except Exception:
        logger.debug("preset transition notify failed for key=%s", key, exc_info=True)


def _read_stamp(key: str) -> str:
    value = ConfigSetting.objects.get_effective(key)
    return value if isinstance(value, str) else ""


def _write_stamp(key: str, value: str) -> None:
    if value:
        ConfigSetting.objects.set_value(key, value)
    else:
        ConfigSetting.objects.clear(key)


def _pending() -> bool:
    return DBTaskResult.objects.filter(task_path=preset_transitions.module_path, status=TaskResultStatus.READY).exists()


@task(queue_name=LOOPS_QUEUE)
def preset_transitions() -> dict[str, Any]:
    """One transition fire: apply side-effects for any switch, then re-schedule this chain.

    Self-dedups first (another pending fire carries the chain), mirroring the
    ``usage_window_recovery`` contract so an at-least-once redelivery collapses to
    one. Always re-schedules, so the chain keeps polling for the next boundary.
    """
    if _pending():
        return {"deduped": 1}
    now = timezone.now()
    try:
        outcome = apply_preset_transition(now)
    except Exception:
        logger.warning("preset transition pass failed — will retry next fire", exc_info=True)
        outcome = {"error": 1}
    preset_transitions.using(run_after=timezone.now() + dt.timedelta(seconds=TRANSITION_POLL_SECONDS)).enqueue()
    return outcome


def ensure_preset_transitions_chain() -> None:
    """Seed the transition chain head if absent — self-perpetuating after (worker startup)."""
    if not _pending():
        preset_transitions.using(run_after=timezone.now() + dt.timedelta(seconds=TRANSITION_POLL_SECONDS)).enqueue()
