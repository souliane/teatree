"""Detect which mechanism drives ticks for a claiming loop session (PR-26 / M9).

A loop lease says WHO owns a slot; :func:`detect_driver` resolves WHAT actually
fires its ticks so the ownership layer can register it and warn loudly when no
driver is present (a DRIVERLESS slot looks healthy but never ticks). It lives on
the loop side because core must not import ``teatree.loop`` — and the management
commands that claim ownership already reach into ``teatree.loop``.

Substrate-agnostic: the probes read the LIVE fleet-admission verdict and the LIVE
worker flock, so a slot claimed while the fleet admits work but no worker is yet
running detects as driverless and says so.
"""

from teatree.core.models import LoopDriver
from teatree.loops.enable_verdict import fleet_admits_work
from teatree.utils.singleton import WORKER_SINGLETON, flock_is_held


def detect_driver() -> str:
    """Resolve the tick driver, or ``""`` (driverless).

    :data:`~teatree.core.models.LoopDriver.LOOP_RUNNER` iff the active preset admits
    work AND a live worker holds the ``WORKER_SINGLETON`` kernel flock. A fleet that
    admits work with a FREE flock is NOT ``loop_runner`` — that is precisely the
    "work admitted but nothing running" hole the DRIVERLESS warning must name. An
    attended session holding the loop slot drives nothing.

    ``external`` is never auto-detected — a foreign scheduler is invisible to
    teatree, so it is set only via an explicit ``--driver external`` override.
    Detection NEVER raises into a claim: a probe failure reads as driverless.
    """
    try:
        if not fleet_admits_work():
            return ""
        # Kernel-flock truth, not ``read_pid`` — a recycled pid can never fake a live worker.
        return LoopDriver.LOOP_RUNNER.value if flock_is_held(WORKER_SINGLETON) else ""
    except Exception:  # noqa: BLE001 — a verdict/flock read failure is not a live worker
        return ""
