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
2. posts ONE Slack line per switch when the resolved mode changed since the last
    stamp.

The transition stamp is internal runtime state kept in a ``ConfigSetting`` row
(no extra migration): the last-applied mode name. Fail-soft throughout — any error
is logged and the chain re-schedules.
"""

import datetime as dt
import logging
from typing import Any

from django.utils import timezone
from django_tasks.base import TaskResultStatus
from django_tasks_db.models import DBTaskResult

from teatree.core.mode_resolution import resolve_active_mode
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.models import ConfigSetting, ModeOverride
from teatree.core.notify import NotifyKind, notify_user
from teatree.core.task_contract import TaskOutcome, task
from teatree.loop.preset_resolution import ActivePreset, resolve_active_preset
from teatree.loops.timer_chains import LOOPS_QUEUE

logger = logging.getLogger(__name__)

TRANSITION_POLL_SECONDS = 60

#: Stamps the PRESET layer (override / schedule slot). Drives the deferred-question drain
#: and the one Slack line per switch, both of which are about the OWNER's reachability —
#: so it must not fire on a mode change the owner never made.
_STAMP_KEY = "loop_preset_transition_stamp"
#: Stamps the resolved MODE, which the preset layer cannot see: an L0 ``default_mode``
#: change moves the mask while ``resolve_active_preset`` returns the same ``None`` before
#: and after, so the reconcile chokepoint keyed on the preset stamp never fired (#4196).
#: Separate from :data:`_STAMP_KEY` on purpose — this one drives ONLY the chain reconcile.
_MODE_STAMP_KEY = "loop_mode_transition_stamp"


def apply_preset_transition(now: dt.datetime) -> dict[str, Any]:
    """Run one transition pass: reap expired override, reconcile chains, drain + Slack line.

    Two stamps, because two different things change on different axes. The PRESET stamp
    gates the owner-facing side effects (drain + Slack line); the MODE stamp gates the
    chain reconcile, because membership follows the resolved mode and that moves on
    inputs the preset layer is blind to. The presence upgrade is deliberately NOT one of
    them: membership closes over it (:meth:`~teatree.loops.enable_verdict.EnablePlanes.admits_any_mask`)
    precisely because it has no transition to stamp.

    Idempotent: with nothing changed since the last stamps, only the expired-override reap
    runs and the pass is otherwise a no-op. Fail-soft — a side-effect failure is
    logged and never propagates (resolution is unaffected either way).
    """
    reaped = _reap_expired_overrides(now)
    outcome: dict[str, Any] = {"reaped": reaped}

    current_mode = resolve_active_mode(now).name
    if current_mode != _read_stamp(_MODE_STAMP_KEY):
        _reconcile_timer_chains()
        _write_stamp(_MODE_STAMP_KEY, current_mode)
        outcome["reconciled"] = current_mode

    active = resolve_active_preset(now)
    current_name = active.preset.name if active is not None else ""
    prior_name = _read_stamp(_STAMP_KEY)
    if current_name == prior_name:
        outcome.setdefault("unchanged", 1)
        return outcome

    _post_switch_line(active, now)
    _write_stamp(_STAMP_KEY, current_name)
    outcome["switched"] = current_name
    return outcome


def _reconcile_timer_chains() -> None:
    """Re-head / prune the loop-timer chains the switch just changed membership of (#4185).

    Chain membership follows the resolved MODE, so a switch that forces a loop ON leaves
    it driverless and one that masks a loop OFF leaves a chain to prune. The 5-minute
    reconcile chain would close both eventually; this 60s chain is the switch's own
    chokepoint, so the new membership takes effect at the switch. Fail-open — a
    reconcile failure never blocks the transition.
    """
    from teatree.loops.timer_reconciler import ensure_loop_timers  # noqa: PLC0415 — deferred: cycle-safe

    try:
        ensure_loop_timers()
    except Exception as exc:  # noqa: BLE001 — reconciliation is best-effort; never block the transition
        logger.warning("preset-transition timer reconcile failed: %s", exc)


def _reap_expired_overrides(now: dt.datetime) -> int:
    deleted, _ = ModeOverride.objects.filter(until__isnull=False, until__lte=now).delete()
    return deleted


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


@task(outcome=TaskOutcome.OK_FLAG, queue_name=LOOPS_QUEUE)
def preset_transitions() -> dict[str, Any]:
    """One transition fire: apply side-effects for any switch, then re-schedule this chain.

    Self-dedups first (another pending fire carries the chain), mirroring the
    ``usage_window_recovery`` contract so an at-least-once redelivery collapses to
    one. Always re-schedules, so the chain keeps polling for the next boundary.
    """
    if _pending():
        return {"ok": True, "deduped": 1}
    now = timezone.now()
    try:
        outcome = apply_preset_transition(now)
    except Exception as exc:
        logger.warning("preset transition pass failed — will retry next fire", exc_info=True)
        outcome = {"ok": False, "detail": str(exc)}
    else:
        outcome["ok"] = True
    preset_transitions.using(run_after=timezone.now() + dt.timedelta(seconds=TRANSITION_POLL_SECONDS)).enqueue()
    return outcome


def ensure_preset_transitions_chain() -> None:
    """Seed the transition chain head if absent — self-perpetuating after (worker startup)."""
    if not _pending():
        preset_transitions.using(run_after=timezone.now() + dt.timedelta(seconds=TRANSITION_POLL_SECONDS)).enqueue()
