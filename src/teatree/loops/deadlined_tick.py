"""Deadlined tick subprocesses in their own process group — the shared runner.

Both loop drivers spawn a tick as a SUBPROCESS rather than calling it in-process:
:func:`teatree.loops.timer_chains.loop_timer` for a live-tick loop and
:func:`teatree.loops.off_live_tick_driver.drive_off_live_tick_loops` for the heavy
``off_live_tick`` passes. Standard over clever — a subprocess isolates a crash or a
hang from the worker executor thread and gives an OS-level kill boundary an in-process
``call_command`` cannot, so one wedged tick costs one executor slot for at most its
deadline instead of forever.

Every spawn leads its own session (``start_new_session=True``), so the deadline kills
the WHOLE group — the tick plus any grandchildren — and never strands children. Live
groups are registered in a lock-guarded process-global set while they run, because the
tick runs in an executor thread while the worker's shutdown runs in the supervisor
thread: :func:`kill_live_tick_process_groups` is what that shutdown calls AFTER its
join timeout, so a tick the join could not reach is killed rather than orphaned.
"""

import logging
import os
import signal
import sys
import threading
from typing import TypedDict

from teatree.core.session_identity import runner_identity_env
from teatree.utils.run import Popen, TimeoutExpired, spawn_session_leader

logger = logging.getLogger(__name__)

#: Set in the deadlined tick subprocess's environment so the ``loops_tick`` command can
#: ``os._exit`` right after rendering — a hung NON-daemon scanner thread would otherwise
#: block interpreter shutdown (its ``ThreadPoolExecutor`` atexit join), pinning the
#: subprocess (and one scarce ``loops`` executor slot) until the outer deadline SIGKILL.
#: Only the spawned subprocess carries it — an in-process ``call_command`` never does, so
#: tests never trip the hard exit.
TICK_SUBPROCESS_ENV_MARKER = "T3_LOOPS_TICK_SUBPROCESS"


class TickOutcome(TypedDict):
    """The result of one deadlined subprocess tick."""

    timed_out: bool
    returncode: int | None


def _tick_argv(name: str) -> list[str]:
    """The subprocess argv for one per-loop tick — ``python -m teatree loops_tick --loop <name>``."""
    return [sys.executable, "-m", "teatree", "loops_tick", "--loop", name]


def tick_subprocess_env() -> dict[str, str]:
    """The environment every worker-spawned tick subprocess runs under.

    One seam so the runner's identity and the hard-exit marker are decided in a
    single place rather than inline at the spawn. The identity is the crux: the
    loop runner is a long-lived daemon with NO Claude session, so without an
    explicit principal ``current_session_id()`` fell through to the loop
    registry's ``t3-loop-tick-owner`` record — a shared file every SessionStart
    rewrites — and the runner's identity silently rotated between its own ticks.
    Each rotation made the next tick a non-owner of the lease its own previous
    tick had just taken, so the loop SKIPped until the lease TTL lapsed and ran
    once per TTL instead of once per cadence.
    """
    return {**os.environ, TICK_SUBPROCESS_ENV_MARKER: "1", **runner_identity_env(os.getpid())}


#: The process-group ids of every tick subprocess currently in flight, so the
#: worker's shutdown can SIGKILL any the executor-join timeout left orphaned. Keyed
#: by pgid (a session leader's pgid == its own pid). The tick runs in an executor
#: thread while the shutdown runs in the supervisor thread, so the set is lock-guarded.
_LIVE_TICK_PGIDS: set[int] = set()
_LIVE_TICK_LOCK = threading.Lock()


def _register_tick_pgid(pgid: int) -> None:
    with _LIVE_TICK_LOCK:
        _LIVE_TICK_PGIDS.add(pgid)


def _unregister_tick_pgid(pgid: int) -> None:
    with _LIVE_TICK_LOCK:
        _LIVE_TICK_PGIDS.discard(pgid)


def kill_live_tick_process_groups() -> list[int]:
    """SIGKILL every in-flight tick process group; return the pgids signalled.

    The worker's shutdown daemon-joins its executors with a short timeout but that
    join does not reach a tick subprocess: a kill-switch flip or a SIGTERM mid-tick
    tears down the executor thread that owned the deadline, orphaning the tick with
    no deadline owner (a no-zombie violation). This is called AFTER the join timeout
    so any still-running tick group is killed rather than left orphaned.
    """
    with _LIVE_TICK_LOCK:
        pgids = list(_LIVE_TICK_PGIDS)
    for pgid in pgids:
        _killpg(pgid)
        _unregister_tick_pgid(pgid)
    return pgids


def run_deadlined_argv(
    argv: list[str], *, label: str, deadline: float, env: dict[str, str] | None = None
) -> TickOutcome:
    """Run *argv* as a deadlined subprocess in its OWN process group.

    On deadline expiry the whole group is ``SIGKILL``-ed, so a hung run can never
    outlive its deadline or strand children; the group is registered while it runs so
    the worker's shutdown can kill it too. *label* names the run in the
    deadline-exceeded log line, and *env* replaces the child's environment when given
    (``None`` inherits this process's).
    """
    proc = spawn_session_leader(argv, env=env)
    pgid = _tick_pgid(proc)
    if pgid is not None:
        _register_tick_pgid(pgid)
    try:
        returncode = proc.wait(timeout=deadline)
    except TimeoutExpired:
        _kill_process_group(proc)
        logger.warning("%s exceeded its %.0fs deadline — killed the process group", label, deadline)
        return {"timed_out": True, "returncode": None}
    finally:
        if pgid is not None:
            _unregister_tick_pgid(pgid)
    return {"timed_out": False, "returncode": returncode}


def run_deadlined_tick(name: str, *, deadline: float) -> TickOutcome:
    """Run one per-loop live tick (``python -m teatree loops_tick --loop <name>``), deadlined."""
    return run_deadlined_argv(
        _tick_argv(name),
        label=f"loop_timer {name!r} tick",
        deadline=deadline,
        env=tick_subprocess_env(),
    )


def _tick_pgid(proc: Popen[str]) -> int | None:
    """The tick subprocess's own process-group id, or ``None`` if it already exited."""
    try:
        return os.getpgid(proc.pid)
    except ProcessLookupError:
        return None


def _killpg(pgid: int) -> None:
    """SIGKILL a whole process group; best-effort, never raise.

    Tolerates a group that is already gone (``ProcessLookupError``) and one whose
    leader's pid was recycled to a foreign-owned process (``PermissionError`` / EPERM)
    — in the shutdown sweep such a pgid is no longer our tick, and a single un-killable
    group must not abort killing the others.
    """
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _kill_process_group(proc: Popen[str]) -> None:
    """SIGKILL the subprocess's whole process group and reap it, tolerating a dead child."""
    pgid = _tick_pgid(proc)
    if pgid is None:
        return
    _killpg(pgid)
    try:
        proc.wait(timeout=10)
    except TimeoutExpired:
        logger.exception("loop tick process group for pid %s did not die after SIGKILL", proc.pid)
