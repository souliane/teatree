"""Stop self-pump gate for the durable DB ``LoopState`` 'pause everything' (#1913).

Extracted from :mod:`hook_router` (it is over the module-health LOC cap and may
only shrink): the in-session Stop self-pump must honour a durable DB pause of
the core ``dispatch`` loop. This DB ``LoopState`` tier is the SINGLE control
plane the self-pump consults (loop control is ``/loops`` + the DB only; there is
no env kill-switch). Keeping it in a sibling helper means the Stop hook gains the
behaviour without growing the god-module.

The read is a DIRECT stdlib ``sqlite3`` read of the PRIMARY teatree DB via the
Django-free :func:`teatree.config.cold_reader.loop_status` — NO ``t3`` subprocess
and NO ``django.setup()``. The prior implementation shelled out to ``t3 loop
loop-state dispatch --json``; the editable install boots Django in that child
process, so every Stop hook paid a ~3s Django cold-boot on its hot path — half of
the recurring Stop-hook TIMEOUT this change removes. ``cold_reader`` reads the
same ``teatree_loop_state`` row the CLI would (resolving the PRIMARY
``~/.local/share/teatree/db.sqlite3`` even from inside a worktree), so the gate
DECISION is unchanged — only the Django cold-boot is gone. ``src/`` is put on
``sys.path`` for the read via the shared :func:`teatree_src_on_path` bootstrap
(the hook interpreter is a bare ``python3`` that does not carry teatree's venv,
#1314).
"""

from hooks.scripts.managed_repo import teatree_src_on_path

# The core loop the in-session Stop self-pump exists to drive. The
# self-pump re-fires ``t3 loop tick`` + ``claim-next``, which run this loop's
# dispatch fan-out; a durable DB hold on it IS the restart-surviving
# 'pause everything' (#1913). Mirrors :data:`teatree.loops.dispatch.loop.MINI_LOOP`'s name.
_DISPATCH_LOOP_NAME = "dispatch"

# The single durable-runnable status (mirrors ``LoopStatus.ENABLED.value``
# without importing teatree). Any other resolved status — ``paused`` /
# ``disabled`` — is a durable hold that suppresses the self-pump.
_RUNNABLE_STATUS = "enabled"


def db_loop_state_suppresses_self_pump() -> bool:
    """True when the durable DB ``LoopState`` pauses/disables the loop (#1913).

    The DB-backed control tier is the SINGLE control plane: a ``PAUSED`` /
    ``DISABLED`` row on the core ``dispatch`` loop (the loop the self-pump
    drives) means the operator has durably paused the control plane, so the
    in-session Stop self-pump must suppress. Loop control is ``/loops`` + the DB
    only; there is no env kill-switch.

    The read is a DIRECT stdlib ``sqlite3`` read of the ``teatree_loop_state``
    row via the Django-free :func:`teatree.config.cold_reader.loop_status` — no
    ``t3`` subprocess, no ``django.setup()`` on the Stop hook's hot path.

    FAIL OPEN — a Stop hook must be crash-proof: ``cold_reader.loop_status``
    resolves an absent row OR an unreadable/locked DB to the runnable
    ``enabled`` default, so this returns ``False`` (do NOT suppress) and the
    availability / ownership gates still decide — the pump can never crash the
    session on an unreadable control plane.
    """
    return _dispatch_loop_status() != _RUNNABLE_STATUS


def _dispatch_loop_status() -> str:
    """Durable status of the ``dispatch`` loop via the Django-free cold reader.

    Reads the ``teatree_loop_state`` row directly from the PRIMARY sqlite DB
    (``cold_reader.loop_status`` targets the installed ``t3``'s DB even from a
    worktree). Any failure — ``teatree`` not importable, an unreadable / locked
    DB — resolves to ``_RUNNABLE_STATUS`` so the caller fails OPEN (do NOT
    suppress). The ``src/`` bootstrap makes ``teatree`` importable in the bare
    ``python3`` hook interpreter (#1314).
    """
    try:
        with teatree_src_on_path():
            from teatree.config.cold_reader import loop_status  # noqa: PLC0415

            return loop_status(_DISPATCH_LOOP_NAME, default=_RUNNABLE_STATUS)
    except Exception:  # noqa: BLE001 — Stop hook crash-proof: unreadable control plane ⇒ fail open (runnable)
        return _RUNNABLE_STATUS


__all__ = ["db_loop_state_suppresses_self_pump"]
