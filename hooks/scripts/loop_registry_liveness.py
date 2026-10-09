"""Is a loop-registry entry's recorded owner still alive (#4270)?

One concern, lifted out of the shrink-only router: the registry stores a pid, and the
SessionStart election and the loop-driven Stop gates prune by probing it.

Cold-import safe: stdlib only at module top, ``teatree`` imported lazily inside each
function, since hooks run under whatever interpreter the agent harness invokes.
"""

import sys

from hooks.scripts.loop_registry_path import OWNER_LOOP, read_loop_registry


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
    we treat loop ownership as unknown (empty registry) rather than crash
    the session. A ``Stop`` hook must be crash-proof by contract.

    A recorded ``pid_namespace`` is deliberately NOT consulted here (#4270).
    Keeping an entry this reader cannot attribute would be permanent: nothing
    behind this file expires a record — no TTL, no reaper, and only the owning
    session's own SessionEnd deletes one — and a restarted container never
    returns to its old namespace, so :func:`session_owns_loop` and
    ``_session_drives_loop`` would read a dead foreign owner forever, retiring
    the Stop gates for every session on the box. An unknown-owner keep is
    conservative only where something else can eventually say NO.
    """
    try:
        from teatree.utils.singleton import pid_alive  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup
    except ImportError as exc:
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] loop owner prune skipped: teatree unavailable ({exc})",
            file=sys.stderr,
        )
        return {}

    return {
        name: entry
        for name, entry in registry.items()
        if isinstance(entry, dict) and pid_alive(int(entry.get("pid", 0) or 0))
    }


def session_owns_loop(session_id: str) -> bool:
    """Whether *session_id* is the live owner of the host's loop slot — a read, never a claim."""
    owner = prune_dead_owner(read_loop_registry()).get(OWNER_LOOP)
    return owner is not None and owner.get("session_id") == session_id
