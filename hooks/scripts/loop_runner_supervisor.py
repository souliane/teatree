"""SessionStart resurrection of the self-owned loop-runner daemon (#2876 decision 2b).

When ``loop_runner_enabled`` is on and no runner holds the ``loop-runner`` flock,
the SessionStart hook (OS-agnostic — it fires on every Claude session start)
re-spawns a detached ``t3 loop-runner``. This is the "at least one" half of
supervision the at-most-one flock cannot provide: the flock gives at-most-one, the
in-daemon supervisor respawns a crashed beat worker, and this rehydrates the whole
daemon after a full crash / reboot.

A standalone infrastructure hook (like the sibling SessionStart ``bootstrap-cli.sh``)
rather than a router handler: ``hook_router.py`` is a grandfathered shrink-only
god-module, so a new SessionStart trigger lives here instead of growing it.

Default-OFF and crash-proof / fail-open / silent: any failure to bootstrap Django,
read the setting, probe the flock, or spawn yields a no-op — never an exception
into the SessionStart hook. On a fully-headless box with no Claude session ever
opening, the operator starts ``t3 loop-runner`` once from a login profile (a
dotfile, not a system scheduler); this hook only covers the session-present case.
"""

import argparse
import shutil
import subprocess  # noqa: S404 — trusted internal spawn of the `t3` CLI (hook convention)
import sys
from collections.abc import Callable

# Alias the bare and ``hooks.scripts.`` identities so the live hook and a test
# importing either name operate on ONE module object (mirrors loop_registrations).
sys.modules.setdefault("loop_runner_supervisor", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.loop_runner_supervisor", sys.modules[__name__])


def _loop_runner_enabled() -> bool:
    """Whether ``loop_runner_enabled`` resolves on; fail-OFF on any error."""
    try:
        from django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415

        if not bootstrap_teatree_django():
            return False
        from teatree.config import get_effective_settings  # noqa: PLC0415

        return bool(get_effective_settings().loop_runner_enabled)
    except Exception:  # noqa: BLE001 — fast hook must never raise; silent fail-off.
        return False


def _flock_is_free() -> bool:
    """Whether the ``loop-runner`` flock has no live holder; fail-SAFE (not free) on error.

    An uncertain probe returns ``False`` so an ambiguous state never triggers a
    spawn — and even a spurious spawn is harmless (the second runner's singleton
    refuses and exits), so this errs toward not-spawning.
    """
    try:
        from teatree.loops.runner import LOOP_RUNNER_SINGLETON  # noqa: PLC0415
        from teatree.utils.singleton import default_pid_path, read_pid  # noqa: PLC0415

        return read_pid(default_pid_path(LOOP_RUNNER_SINGLETON)) is None
    except Exception:  # noqa: BLE001 — can't tell -> do NOT spawn a possible duplicate.
        return False


def _spawn_loop_runner() -> None:
    """Spawn a detached ``t3 loop-runner`` that outlives this session; a no-op if ``t3`` is absent."""
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return
    subprocess.Popen(  # noqa: S603 — resolved binary, fixed argv, no shell, no user input.
        [t3_bin, "loop-runner"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def resurrect_loop_runner(
    *,
    enabled: Callable[[], bool] = _loop_runner_enabled,
    flock_free: Callable[[], bool] = _flock_is_free,
    spawn: Callable[[], None] = _spawn_loop_runner,
) -> str:
    """Spawn a detached loop-runner iff enabled AND the flock is free; return the action.

    Returns ``"disabled"`` (not opted in), ``"already-running"`` (a runner holds the
    flock), ``"spawned"`` (a fresh daemon was launched), or ``"error"`` (fail-open).
    """
    try:
        if not enabled():
            return "disabled"
        if not flock_free():
            return "already-running"
        spawn()
    except Exception:  # noqa: BLE001 — never raise into the SessionStart hook.
        return "error"
    return "spawned"


def main() -> int:
    """SessionStart hook entry point — resurrect the daemon, always exit 0."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default="")
    parser.parse_args()
    sys.stdin.read()  # drain the payload; the resurrection needs none of it
    resurrect_loop_runner()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
