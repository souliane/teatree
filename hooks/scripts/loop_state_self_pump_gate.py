"""Stop self-pump gate for the durable DB ``LoopState`` 'pause everything' (#1913).

Extracted from :mod:`hook_router` (it is over the module-health LOC cap and may
only shrink): the in-session Stop self-pump must honour a durable DB pause of
the always-on ``dispatch`` loop exactly as it honours ``T3_LOOPS_DISABLED=all``.
Keeping it in a sibling helper means the Stop hook gains the behaviour without
growing the god-module.
"""

from django_bootstrap import bootstrap_teatree_django

# The always-on loop the in-session Stop self-pump exists to drive. The
# self-pump re-fires ``t3 loop tick`` + ``claim-next``, which run this loop's
# dispatch fan-out; a durable DB hold on it IS the restart-surviving
# 'pause everything' the env ``T3_LOOPS_DISABLED=all`` kill-switch could only do
# within a process (#1913). Mirrors :data:`teatree.loops.dispatch.loop.MINI_LOOP`'s name.
_DISPATCH_LOOP_NAME = "dispatch"


def db_loop_state_suppresses_self_pump() -> bool:
    """True when the durable DB ``LoopState`` pauses/disables the loop (#1913).

    The DB-backed control tier is the restart-surviving counterpart of
    ``T3_LOOPS_DISABLED=all``: a ``PAUSED`` / ``DISABLED`` row on the always-on
    ``dispatch`` loop (the loop the self-pump drives) means the operator has
    durably paused the control plane, so the in-session Stop self-pump must
    suppress exactly as it does for the env kill-switch.

    FAIL OPEN — a Stop hook must be crash-proof: an unbootstrappable / absent
    ``teatree`` or any DB read error resolves to ``False`` (do NOT suppress), so
    the env / availability / ownership gates still decide and the pump can never
    crash the session on an unreadable database.
    """
    if not bootstrap_teatree_django():
        return False
    try:
        from teatree.core.models import LoopState  # noqa: PLC0415

        return not LoopState.objects.is_runnable(_DISPATCH_LOOP_NAME)
    except Exception:  # noqa: BLE001 — crash-proof: a DB error must not crash the Stop hook
        return False


__all__ = ["db_loop_state_suppresses_self_pump"]
