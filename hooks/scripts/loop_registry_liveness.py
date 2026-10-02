"""Is a loop-registry entry's recorded owner still alive (#4270)?

One concern, lifted out of the shrink-only router: the registry stores a pid, and the
Stop self-pump's prune probes it. The writer's side of the question lives here too — the
record carries the pid namespace its pid was minted in, which the driver-detection probe
reads before treating that integer as a fact about the owner.

Cold-import safe: stdlib only at module top, ``teatree`` imported lazily inside each
function, since hooks run under whatever interpreter the agent harness invokes.
"""

import sys


def pid_namespace() -> str:
    """This process's pid namespace, ``""`` when ``teatree`` is unimportable from the hook."""
    try:
        from teatree.utils.singleton import (  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup
            current_context,
        )
    except ImportError:
        return ""
    return current_context().pid_namespace


def prune_dead_owner(registry: dict[str, dict]) -> dict[str, dict]:
    """Drop registry entries whose recorded owner pid is no longer alive.

    Reuses the existing ``teatree.utils.singleton.pid_alive`` primitive
    rather than re-implementing pid liveness — the locked design calls
    for preferring the existing singleton/pid mechanism. Imported lazily
    to keep this Django-free hook fast on the common path (mirrors the
    lazy ``teatree.skill_support.deps`` import elsewhere in the router).

    Fail-safe (#810): hooks run under whatever interpreter the agent
    harness invokes; ``teatree`` importability is NOT guaranteed there.
    When the import fails we cannot confirm any owner pid is alive, so
    we treat loop ownership as unknown (empty registry) and let the
    caller skip the self-pump rather than crash the session. A ``Stop``
    hook must be crash-proof by contract.

    The recorded ``pid_namespace`` is deliberately NOT consulted here (#4270).
    Keeping an entry this reader cannot attribute would be permanent: nothing
    behind this file expires a record — no TTL, no reaper, and only the owning
    session's own SessionEnd deletes one — and a restarted container never
    returns to its old namespace, so ``_session_owns_loop`` and
    ``_session_drives_loop`` would read a dead foreign owner forever, retiring
    the Stop gates for every session on the box. An unknown-owner keep is
    conservative only where something else can eventually say NO.
    """
    try:
        from teatree.utils.singleton import pid_alive  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup
    except ImportError as exc:
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] loop self-pump skipped: teatree unavailable ({exc})",
            file=sys.stderr,
        )
        return {}

    return {
        name: entry
        for name, entry in registry.items()
        if isinstance(entry, dict) and pid_alive(int(entry.get("pid", 0) or 0))
    }
