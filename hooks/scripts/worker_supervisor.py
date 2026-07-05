"""SessionStart resurrection of the singleton loop-timer worker (#1796).

When ``loop_runner_enabled`` is on and no worker holds the ``worker`` flock, the
SessionStart hook (OS-agnostic — it fires on every Claude session start) re-spawns
a detached ``t3 worker``. This is the "at least one" half of supervision the
at-most-one flock cannot provide: the flock gives at-most-one, the worker's own
supervisor thread stops the executor pool on a kill-switch flip, and this
rehydrates the whole worker after a full crash / reboot.

A standalone infrastructure hook (like the sibling SessionStart ``bootstrap-cli.sh``)
rather than a router handler: ``hook_router.py`` is a grandfathered shrink-only
god-module, so a new SessionStart trigger lives here instead of growing it.

Default-OFF and crash-proof / fail-open / silent: any failure to read the
``loop_runner_enabled`` flag, probe the flock, or spawn yields a no-op — never an
exception into the SessionStart hook. The enable check boots NO Django (#2879
parity): the DB-home flag is read via the Django-free ``teatree.config.cold_reader``
stdlib-sqlite path, so a fresh, non-engaged session (contra #256) never pays a full
``django.setup()`` on the session-start critical path just to read a default-OFF
flag. On a fully-headless box with no Claude session ever opening, the operator
starts ``t3 worker`` once from a login profile (a dotfile, not a system scheduler);
this hook only covers the session-present case.
"""

import argparse
import os
import shutil
import subprocess  # noqa: S404 — trusted internal spawn of the `t3` CLI (hook convention)
import sys
from collections.abc import Callable

# Alias the bare and ``hooks.scripts.`` identities so the live hook and a test
# importing either name operate on ONE module object (mirrors loop_registrations).
sys.modules.setdefault("worker_supervisor", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.worker_supervisor", sys.modules[__name__])


#: Truthy tokens for the ``T3_LOOP_RUNNER_ENABLED`` env override — mirrors the
#: hot-path ``teatree.config.settings._parse_env_bool`` and the ``autoload`` cold reader.
_ENABLED_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def _enabled_scope_chain() -> tuple[str, ...]:
    """Overlay-then-global scope chain for ``loop_runner_enabled`` (global-only when unset).

    The flag is per-overlay overridable (``config_setting set … --overlay <name>``),
    so an overlay-scope row must win over the global one — exactly as
    ``get_effective_settings`` resolves it. ``T3_OVERLAY_NAME`` names the active
    overlay (the same env var the config resolver keys on); with none set the chain
    is global-only.
    """
    overlay = os.environ.get("T3_OVERLAY_NAME", "").strip()
    return (overlay, "") if overlay else ("",)


def _worker_enabled() -> bool:
    """Whether ``loop_runner_enabled`` resolves on — Django-free cold read, fail-OFF.

    #2879 parity: read the DB-home flag WITHOUT booting Django.
    ``T3_LOOP_RUNNER_ENABLED`` env wins (matching the hot-path
    ``ENV_SETTING_OVERRIDES``); otherwise the ``ConfigSetting`` store is read via the
    stdlib-only ``teatree.config.cold_reader`` (overlay scope, then global — the flag
    is per-overlay overridable), defaulting OFF. A ``[teatree]`` TOML value is DB-home
    and ignored on read, so there is no TOML fallback (as with ``autoload``). Any read
    error → OFF, so a missing/unreadable DB never crashes the session (fail-open).
    """
    env = os.environ.get("T3_LOOP_RUNNER_ENABLED", "").strip().lower()
    if env:
        return env in _ENABLED_TRUTHY
    try:
        from teatree.config.cold_reader import bool_setting  # noqa: PLC0415

        return bool_setting("loop_runner_enabled", default=False, scope_chain=_enabled_scope_chain())
    except Exception:  # noqa: BLE001 — fast hook must never raise; silent fail-off.
        return False


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
    """Spawn a detached ``t3 worker`` that outlives this session; a no-op if ``t3`` is absent."""
    t3_bin = shutil.which("t3")
    if not t3_bin:
        return
    subprocess.Popen(  # noqa: S603 — resolved binary, fixed argv, no shell, no user input.
        [t3_bin, "worker"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def resurrect_worker(
    *,
    enabled: Callable[[], bool] = _worker_enabled,
    flock_free: Callable[[], bool] = _flock_is_free,
    spawn: Callable[[], None] = _spawn_worker,
) -> str:
    """Spawn a detached worker iff enabled AND the flock is free; return the action.

    Returns ``"disabled"`` (not opted in), ``"already-running"`` (a worker holds the
    flock), ``"spawned"`` (a fresh worker was launched), or ``"error"`` (fail-open).
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
    """SessionStart hook entry point — resurrect the worker, always exit 0."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", default="")
    parser.parse_args()
    sys.stdin.read()  # drain the payload; the resurrection needs none of it
    resurrect_worker()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
