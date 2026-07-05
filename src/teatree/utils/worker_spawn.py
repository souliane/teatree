"""Detached spawn of ``t3 worker`` (#1796 / PR-28).

The ONE spawner shared by the SessionStart resurrector
(``hooks/scripts/worker_supervisor.py``) and ``t3 worker ensure`` so the two can
never diverge on how the detached worker is launched. The shell-out goes through the
typed ``teatree.utils.run.spawn_session_leader`` wrapper (the sanctioned egress).
Imported DEFERRED by the cold SessionStart hook, so hook module load pays nothing.
"""

import shutil

from teatree.utils.run import DEVNULL, spawn_session_leader


def spawn_detached_worker() -> bool:
    """Spawn a detached ``t3 worker`` that outlives the caller; ``False`` iff ``t3`` is absent.

    Resolved binary, fixed argv, no shell, no user input. ``spawn_session_leader`` makes
    the worker a session leader (``start_new_session=True``, ``stdin=DEVNULL``) so it
    survives the caller's exit (a SessionStart hook, or the short-lived
    ``t3 worker ensure`` process). The flock singleton the worker acquires makes a
    double-spawn harmless (the second refuses and exits), so the caller need not
    serialize; it should still probe the flock first to avoid the wasted spawn.
    """
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return False
    spawn_session_leader([t3_bin, "worker"], stdout=DEVNULL, stderr=DEVNULL)
    return True
