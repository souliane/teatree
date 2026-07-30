"""Loop-side entry point for the session-scoped identity primitive (#1073).

The precedence lives in exactly ONE place — :mod:`teatree.core.session_identity`
— because the module-boundary graph forbids the core-only
``teatree.outbound_claim`` re-exporter from importing ``teatree.loop``. The loop
callers (``loops_tick``, ``statusline``, ``tick``) reach it through here so the
loop package keeps a self-describing entry point for its own ownership identity.
Nothing below decides anything: each function forwards, so there is one resolver
and one precedence order, never two.

These are call-time DELEGATIONS, deliberately NOT ``from ... import``
re-exports (#3810). A re-export binds the core function OBJECT at import time,
so a module first imported while a test patches
``teatree.core.session_identity.current_session_id`` captures that mock
**permanently**: ``mock.patch`` restores only the attribute it replaced, never
the copies other modules already took. That is precisely how ``t3 teatree
handover whoami`` began reporting a foreign session id for the rest of the
process —
``loop_owner`` pulls ``loop_principal`` through this module inside such a patch,
which made this module the first importer, and ``handover`` then inherited the
dead mock through its own import of the poisoned name. Resolving the attribute
on every call makes that capture impossible and keeps patching either module
equivalent.
"""

from pathlib import Path

from teatree.core import session_identity


def loop_registry_dir() -> Path:
    return session_identity.loop_registry_dir()


def current_session_id() -> str:
    return session_identity.current_session_id()


def current_session_pid() -> int | None:
    return session_identity.current_session_pid()


def loop_principal() -> tuple[str, int | None]:
    return session_identity.loop_principal()


def runner_identity_env(pid: int) -> dict[str, str]:
    return session_identity.runner_identity_env(pid)


__all__ = ["current_session_id", "current_session_pid", "loop_principal", "loop_registry_dir", "runner_identity_env"]
