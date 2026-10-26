"""Detached spawn of ``t3 worker`` (#1796 / PR-28).

The ONE spawner shared by the SessionStart resurrector
(``hooks/scripts/worker_supervisor.py``) and ``t3 worker ensure`` so the two can
never diverge on how the detached worker is launched. The shell-out goes through the
typed ``teatree.utils.run.spawn_session_leader`` wrapper (the sanctioned egress).
Imported DEFERRED by the cold SessionStart hook, so hook module load pays nothing.

The child's stderr is captured in :data:`SPAWN_LOG_PATH` rather than discarded: this
function reports success as soon as the ``t3`` binary resolves, so a worker that dies
during startup (a bad settings module, a failed migration, an import error) would
otherwise leave nothing at all to read. The log is TRUNCATED on every spawn, so it
holds exactly one worker's output and cannot grow across restarts.
"""

import os
import shutil
from pathlib import Path

from teatree.paths import DATA_DIR
from teatree.utils.run import DEVNULL, spawn_session_leader
from teatree.utils.singleton import _worker_control_database

SPAWN_LOG_PATH = DATA_DIR / "worker-spawn.log"
_SPAWN_LOG_TAIL_LINES = 20


def refuse_external_database_under_pytest_home() -> None:
    home = Path.home().resolve()
    pytest_root = next(
        (Path(*home.parts[: index + 1]) for index, part in enumerate(home.parts) if part.startswith("pytest-of-")),
        None,
    )
    if pytest_root is None:
        return
    databases = [_worker_control_database(primary=True)]
    databases.extend(Path(path) for key in ("T3_CONFIG_DB", "T3_CONTROL_DB_DIR") if (path := os.environ.get(key)))
    external = next((path for path in databases if not path.resolve().is_relative_to(pytest_root)), None)
    if external is not None:
        message = f"refusing t3 worker: pytest HOME {home} points at external control database {external}"
        raise RuntimeError(message)


def read_spawn_log_tail(*, lines: int = _SPAWN_LOG_TAIL_LINES) -> str:
    """The last ``lines`` of the last spawned worker's stderr, or ``""`` when there is none."""
    if not SPAWN_LOG_PATH.is_file():
        return ""
    captured = SPAWN_LOG_PATH.read_text(encoding="utf-8", errors="replace").rstrip().splitlines()
    return "\n".join(captured[-lines:])


def spawn_detached_worker() -> bool:
    """Spawn a detached ``t3 worker`` that outlives the caller; ``False`` iff ``t3`` is absent.

    Resolved binary, fixed argv, no shell, no user input. ``spawn_session_leader`` makes
    the worker a session leader (``start_new_session=True``, ``stdin=DEVNULL``) so it
    survives the caller's exit (a SessionStart hook, or the short-lived
    ``t3 worker ensure`` process). The flock singleton the worker acquires makes a
    double-spawn harmless (the second refuses and exits), so the caller need not
    serialize; it should still probe the flock first to avoid the wasted spawn.

    ``True`` means "a worker process was launched", NOT "a worker is running" — only the
    flock probe proves that (``teatree.loop.worker_lifecycle.wait_for_new_holder``). When
    it does not come up, :func:`read_spawn_log_tail` has the child's own stderr.
    """
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return False
    SPAWN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Truncating open: the parent's handle is closed right after the fork dups it into
    # the child, so the file holds this worker's output alone.
    with SPAWN_LOG_PATH.open("w", encoding="utf-8") as spawn_log:
        spawn_session_leader([t3_bin, "worker"], stdio=(DEVNULL, DEVNULL, spawn_log))
    return True
