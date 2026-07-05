"""Detect which mechanism drives ticks for a claiming loop session (PR-26 / M9).

A loop lease says WHO owns a slot; :func:`detect_driver` resolves WHAT actually
fires its ticks so the ownership layer can register it and warn loudly when no
driver is present (a DRIVERLESS slot looks healthy but never ticks). It lives on
the loop side because core must not import ``teatree.loop`` — and the management
commands that claim ownership already reach into ``teatree.loop``.

Substrate-agnostic: the probes read the LIVE ``loop_runner_enabled`` setting and
the LIVE worker flock, so the same code is correct before and after the
loop-runner default flip — only the observed distribution of values changes.
This is the observability net for that flip (a slot claimed while the worker is
enabled-but-not-yet-running detects as driverless and says so).
"""

from teatree.config.resolution import get_effective_settings
from teatree.core.models import LoopDriver
from teatree.core.session_identity import owner_record
from teatree.utils.singleton import WORKER_SINGLETON, flock_is_held, pid_alive


def detect_driver(session_id: str) -> str:
    """Resolve the tick driver for ``session_id``, or ``""`` (driverless).

    Deterministic precedence, each probe cheap and fail-safe-to-``""`` (any probe
    exception is swallowed — detection NEVER raises into a claim):

    - :data:`~teatree.core.models.LoopDriver.LOOP_RUNNER` iff the
        ``loop_runner_enabled`` kill-switch resolves ON AND a live worker holds the
        ``WORKER_SINGLETON`` kernel flock. A flag that is ON with a FREE flock is
        NOT ``loop_runner`` — that is precisely the "worker enabled but not running"
        hole the DRIVERLESS warning must name.
    - :data:`~teatree.core.models.LoopDriver.SELF_PUMP` iff the loop-registry
        ``t3-loop-tick-owner`` record names THIS ``session_id`` with a live pid (the
        Stop self-pump keeps the owning session alive to fire ticks).
    - else ``""`` (driverless).

    ``external`` is never auto-detected — a foreign scheduler is invisible to
    teatree, so it is set only via an explicit ``--driver external`` override.

    ``session_id`` is passed in (never re-resolved internally) so the self-pump
    probe compares against the SAME identity the claim uses — a ``t3 loop claim``
    running in a Bash-tool subprocess resolves its id via the #1107 registry
    fallback, and re-resolving here could compare against a different one.
    """
    if _worker_is_driving():
        return LoopDriver.LOOP_RUNNER.value
    if _self_pump_is_driving(session_id):
        return LoopDriver.SELF_PUMP.value
    return ""


def _worker_is_driving() -> bool:
    """Whether the loop runner is both ENABLED and actually holding the worker flock."""
    try:
        if not get_effective_settings().loop_runner_enabled:
            return False
        # Kernel-flock truth, not ``read_pid`` — a recycled pid can never fake a live
        # worker. A flag ON with a FREE flock is the "enabled but not running" hole.
        return flock_is_held(WORKER_SINGLETON)
    except Exception:  # noqa: BLE001 — a settings/flock read failure is not a live worker
        return False


def _self_pump_is_driving(session_id: str) -> bool:
    """Whether the loop-registry tick-owner record names ``session_id`` with a live pid."""
    if not session_id:
        return False
    try:
        record = owner_record()
    except Exception:  # noqa: BLE001 — an unreadable registry is not a live self-pump
        return False
    if not record or record.get("session_id") != session_id:
        return False
    return _pid_is_alive(record.get("pid"))


def _pid_is_alive(pid: object) -> bool:
    """Whether ``pid`` (an int or a digit string from the registry) names a live process."""
    if isinstance(pid, bool) or not isinstance(pid, int | str):
        return False
    text = str(pid).strip()
    if not text.isdigit() or int(text) <= 0:
        return False
    try:
        return pid_alive(int(text))
    except Exception:  # noqa: BLE001 — a liveness-probe failure is not a live self-pump
        return False
