"""SessionStart resurrection of the singleton loop-timer worker (#1796).

When no worker holds the ``worker`` flock, the SessionStart hook (OS-agnostic — it fires
on every Claude session start) re-spawns a detached ``t3 worker``. This is the "at least
one" half of supervision the at-most-one flock cannot provide: the flock gives
at-most-one, the worker's own supervisor thread quiesces its executor pool while the
active preset admits nothing, and this rehydrates the whole worker after a full crash /
reboot.

A standalone infrastructure hook (like the sibling SessionStart ``bootstrap-cli.sh``)
rather than a router handler: ``hook_router.py`` is a grandfathered shrink-only
god-module, so a new SessionStart trigger lives here instead of growing it.

It reads no posture of its own, and must not: a worker whose preset admits nothing parks
with its executors stopped and its process alive, because the schedule boundary that
re-admits work is driven from that process — so "should anything run" is the worker's
question, and this hook only guarantees there IS a worker to ask it. Crash-proof /
fail-open / silent throughout: a failure to probe the flock or spawn yields a no-op,
never an exception into the SessionStart hook, and it boots NO Django (#2879 parity). On
a fully-headless box with no Claude session ever opening, the operator starts
``t3 worker`` once from a login profile (a dotfile, not a system scheduler); this hook
only covers the session-present case.
"""

import argparse
import sys
from collections.abc import Callable

# Alias the bare and ``hooks.scripts.`` identities so the live hook and a test
# importing either name operate on ONE module object (mirrors loop_registrations).
sys.modules.setdefault("worker_supervisor", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.worker_supervisor", sys.modules[__name__])


def _flock_is_free() -> bool:
    """Whether the ``worker`` flock has no live holder; fail-SAFE (not free) on error.

    Probes the KERNEL ``flock`` state, not the recorded pid: a ``read_pid`` liveness
    probe treats a RECYCLED pid (an unrelated live process that reused a crashed
    worker's pid) as a live holder and suppresses resurrection — and the reconciler —
    indefinitely. The ``flock`` probe reflects the actual lock, so a dead worker's
    freed flock always reads free. An uncertain probe returns ``False`` so an
    ambiguous state never triggers a spawn — and even a spurious spawn is harmless
    (the second worker's own flock singleton refuses and exits), so this errs toward
    not-spawning.
    """
    try:
        from teatree.utils.singleton import (  # noqa: PLC0415 — deferred: cold-hook safe, no teatree at import
            WORKER_SINGLETON,
            flock_is_held,
        )

        return not flock_is_held(WORKER_SINGLETON)
    except Exception:  # noqa: BLE001 — can't tell -> do NOT spawn a possible duplicate.
        return False


def _spawn_worker() -> None:
    """Spawn a detached ``t3 worker`` that outlives this session; a no-op if ``t3`` is absent.

    Delegates to the ONE stdlib-only spawner ``teatree.utils.worker_spawn`` shared with
    ``t3 worker ensure`` so the two can never diverge on how the detached worker launches.
    """
    from teatree.utils.worker_spawn import spawn_detached_worker  # noqa: PLC0415 (deferred: cold-hook-safe import)

    spawn_detached_worker()


def resurrect_worker(
    *,
    flock_free: Callable[[], bool] = _flock_is_free,
    spawn: Callable[[], None] = _spawn_worker,
) -> str:
    """Spawn a detached worker iff the flock is free; return the action.

    Returns ``"already-running"`` (a worker holds the flock), ``"spawned"`` (a fresh
    worker was launched), or ``"error"`` (fail-open).
    """
    try:
        if not flock_free():
            return "already-running"
        spawn()
    except Exception:  # noqa: BLE001 — never raise into the SessionStart hook.
        return "error"
    return "spawned"


def main() -> int:
    """SessionStart hook entry point — resurrect the worker, always exit 0."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default="")
    parser.parse_args()
    sys.stdin.read()  # drain the payload; the resurrection needs none of it
    resurrect_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
