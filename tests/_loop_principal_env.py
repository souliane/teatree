"""Shared test-infra helper: pin the loop principal through its env seam (souliane/teatree#3810).

``mock.patch`` restores only the attribute it replaced, never the copies other
modules already took. So patching an identity resolver leaks PERMANENTLY into
any module whose *first* import happens while the patch is live, and Django
imports management commands lazily — which is how the #3810 CI red arose: ``t3
handover whoami`` reported a loop-ownership test's ``sess-x`` for the rest of
the xdist worker, and ``handover create`` then exited 1 on the empty payload.

``teatree.loop.session_identity`` now delegates on every call, which closes that
door for ``teatree.core.session_identity`` patches. It does NOT close the door
for patches of the loop entry point itself: ``handover`` still binds
``current_session_id`` from it with a module-level ``from ... import``, so
``mock.patch("teatree.loop.session_identity.current_session_id")`` reproduces
the identical poisoning. ``TestPinLeavesNoResidue`` pins exactly that.

The env vars are the resolvers' own documented override seam
(:data:`~teatree.core.session_identity.SESSION_ID_ENV_VARS`), so driving them
pins the identity while replacing no module attribute — no door to leave open,
and the integration-shaped choice ``tests/CLAUDE.md`` asks for.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from teatree.core.session_identity import RUNNER_PID_ENV, RUNNER_SESSION_ENV, SESSION_ID_ENV_VARS
from teatree.utils.env import patched_environ

#: The durable-session-pid override ``current_session_pid()`` reads first.
SESSION_PID_ENV = "T3_LOOP_SESSION_PID"

#: Both resolvers fall back to the loop registry — a real file on a dev box, and
#: one every ``SessionStart`` hook rewrites. Point it at a path that cannot exist
#: so an ambient registry can never leak a foreign identity into a pinned test.
_NO_REGISTRY_DIR = "/nonexistent-loop-registry-dir"

_PINNED_VARS = (*SESSION_ID_ENV_VARS, RUNNER_SESSION_ENV, RUNNER_PID_ENV, SESSION_PID_ENV)


@contextmanager
def pinned_loop_principal(session_id: str = "", *, pid: int | None = None) -> Iterator[None]:
    """Resolve the loop principal to (*session_id*, *pid*) for the duration.

    Every source the resolvers consult is neutralised first, so the pin holds
    whatever the ambient environment carries — a test run from inside a live
    Claude session, or from inside a ``t3 worker`` tick subprocess that exports
    the runner principal, resolves the same as in a clean CI container.

    ``session_id=""`` pins the anonymous principal (the #1107 pure-cron case).
    ``pid=None`` leaves the owner pid unresolvable, so a pid-anchored caller
    falls back to ``os.getppid()``.
    """
    overrides = {"T3_LOOP_REGISTRY_DIR": _NO_REGISTRY_DIR}
    if session_id:
        overrides[SESSION_ID_ENV_VARS[0]] = session_id
    if pid is not None:
        overrides[SESSION_PID_ENV] = str(pid)
    with patched_environ(overrides, remove=tuple(n for n in _PINNED_VARS if n not in overrides)):
        yield
