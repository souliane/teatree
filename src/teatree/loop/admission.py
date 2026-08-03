"""Loop-side assembly of the admission-governor decision (#3644).

The pure decision lives in :mod:`teatree.core.admission_governor`; this module wires
its signals from the loop layer (where the resource readers and the task queue live)
and makes the verdict VISIBLE. It is asked at the moment of an admission decision —
event-driven, never a polling timer — and it never consults a model.

Brake state (the hysteresis input) rides in the existing ``tick-meta.json`` sidecar
beside the admit budget, so the governor needs no migration and can be removed by
deleting this module plus its one call site.
"""

import datetime as dt
import json
import logging
from pathlib import Path

from teatree.core.admission_governor import (
    AdmissionDecision,
    YieldSignal,
    decide_admission,
    governor_enabled,
    read_machine_signal,
    read_quota_signal,
)

logger = logging.getLogger(__name__)

#: The tick-meta key carrying the previous decision's brake state.
BRAKED_KEY = "admission_governor_braked"

#: Terminal tasks inside this window feed the yield-per-token signal.
YIELD_WINDOW = dt.timedelta(hours=6)


def _meta_path(statusline_path: Path) -> Path:
    return statusline_path.with_name("tick-meta.json")


def read_braked(*, statusline_path: Path) -> bool:
    """The previous decision's brake state; ``False`` on any unreadable sidecar.

    Losing the brake state costs one extra evaluation at the high watermark, never a
    wrong denial — so the uncertain answer is the un-braked one.
    """
    try:
        payload = json.loads(_meta_path(statusline_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get(BRAKED_KEY)) if isinstance(payload, dict) else False


def write_braked(*, braked: bool, statusline_path: Path) -> None:
    """Persist the brake state, merging into the tick-meta sidecar (never clobbering it)."""
    meta_path = _meta_path(statusline_path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload[BRAKED_KEY] = bool(braked)
    try:
        meta_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    except OSError:
        logger.exception("admission governor could not persist its brake state to %s", meta_path)


def read_yield_signal(now: dt.datetime | None = None) -> YieldSignal:
    """Terminal task outcomes in the recent window — high burn producing nothing.

    Windowed on ``created_at``: ``Task`` carries no completion timestamp, and a task
    created inside the window has necessarily spent its tokens inside it, which is the
    quantity the yield ratio is about.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django app-registry read at call time

    from teatree.core.models import Task  # noqa: PLC0415 — deferred: same

    since = (now or timezone.now()) - YIELD_WINDOW
    recent = Task.objects.filter(created_at__gte=since)
    return YieldSignal(
        completed=recent.filter(status=Task.Status.COMPLETED).count(),
        failed=recent.filter(status=Task.Status.FAILED).count(),
    )


def governor_verdict(*, statusline_path: Path, static_ceiling: int | None = None) -> AdmissionDecision | None:
    """The live admission verdict, or ``None`` when the governor is off or unavailable.

    ``None`` means "the governor has no opinion" — the caller keeps its pre-governor
    static behaviour. That is the kill-switch path (``admission_governor_enabled``
    false) and the degraded path (a signal read raised), never a silent denial: a
    governor that cannot read its own signals must not wedge the factory.
    """
    try:
        if not governor_enabled():
            return None
        decision = decide_admission(
            quota=read_quota_signal(),
            machine=read_machine_signal(),
            yield_signal=read_yield_signal(),
            braked=read_braked(statusline_path=statusline_path),
            static_ceiling=static_ceiling,
        )
    except Exception:
        logger.exception("admission governor probe failed — falling back to the static ceiling")
        return None
    write_braked(braked=decision.braked, statusline_path=statusline_path)
    if not decision.admit:
        # Never silent: a governor that refuses without saying so recreates the class of
        # bug that hid a dead merge loop for weeks.
        logger.warning("admission governor DENIED a new admission: %s", decision.reason)
    return decision


__all__ = [
    "BRAKED_KEY",
    "YIELD_WINDOW",
    "governor_verdict",
    "read_braked",
    "read_yield_signal",
    "write_braked",
]
