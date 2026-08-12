"""Is a loop-registry entry's recorded owner still alive (#4270)?

One concern, lifted out of the shrink-only router: the registry stores a pid, and
every decision that reads it — the Stop self-pump's prune, the driver-detection
self-pump probe — must first know whose pid namespace that integer names. The
writer's side of that question lives here too, so the recorded namespace and the
predicate that reads it cannot drift apart across a file boundary.

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

    An entry recorded in ANOTHER pid namespace is kept (#4270): its pid names
    whatever occupies that integer here, so probing it would drop the loop's
    live owner on a collision. An entry carrying no namespace is probed as
    before — the pid is the only evidence there is, and the record regains a
    namespace on the owner's next SessionStart.
    """
    try:
        from teatree.core.loop_lease_liveness import (  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup
            namespace_is_attributable,
        )
        from teatree.utils.singleton import pid_alive  # noqa: PLC0415 — deferred: cold-hook import after sys.path setup
    except ImportError as exc:
        print(  # noqa: T201 — hook stderr is the module's logging channel
            f"[hook_router] loop self-pump skipped: teatree unavailable ({exc})",
            file=sys.stderr,
        )
        return {}

    def _owner_is_live(entry: dict) -> bool:
        if not namespace_is_attributable(str(entry.get("pid_namespace") or "")):
            return True
        return pid_alive(int(entry.get("pid", 0) or 0))

    return {name: entry for name, entry in registry.items() if isinstance(entry, dict) and _owner_is_live(entry)}
