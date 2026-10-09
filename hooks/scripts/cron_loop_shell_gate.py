"""PreToolUse: block-cron-loop-shell — refuse a cron/wakeup that drives a t3 loop.

The owner session is periodically asked (by its own session-setup prose) to
register the reactive infra slots as harness crons. On a box whose ``t3 worker``
is alive that is pure waste: the worker already drives those cycles, and every
scheduled tick stands down against the worker singleton. The same mistake
recurred three times from a durable memory, so it is enforced here rather than
written down again (#2663).

The decision core is :mod:`teatree.core.gates.cron_loop_shell_gate`; this module
supplies the worker-liveness probe, the per-call escape, the kill-switch, and
routes the deny through the router's shared ``_fail_open_or_deny`` chain
(self-rescue allowlist + master fail-open + circuit breaker all apply).

NEVER-LOCKOUT: a per-call ``[cron-loop-ok: <reason>]`` token (in the scheduled
prompt or a wakeup's reason), the ``[teatree] cron_loop_shell_gate_enabled =
false`` kill-switch (``t3 <overlay> gate cron-loop-shell disable``), and the
unconditional fail-open chain all keep this gate from wedging a session.
"""

import sys
from typing import TYPE_CHECKING

from hooks.scripts.managed_repo import teatree_src_on_path

if TYPE_CHECKING:
    from teatree.core.gates.cron_loop_shell_gate import CronLoopShellFinding

# Alias the bare and ``hooks.scripts.`` identities so the handler the router
# registers and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("cron_loop_shell_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.cron_loop_shell_gate", sys.modules[__name__])

_SCHEDULING_TOOLS = frozenset({"CronCreate", "ScheduleWakeup"})

#: Fields a scheduling call carries text in — the prompt is what fires, the
#: reason is where a ScheduleWakeup caller naturally writes the escape token.
_TEXT_FIELDS = ("prompt", "reason")


def _cron_loop_shell_gate_enabled() -> bool:
    """Whether the gate is enabled (default True); an explicit ``false`` is the kill-switch."""
    from hooks.scripts.hook_router import _teatree_bool_setting  # noqa: PLC0415 deferred back-import

    return _teatree_bool_setting("cron_loop_shell_gate_enabled", default=True)


def _flock_held() -> bool:
    """Whether a live process holds the worker singleton flock, right now."""
    with teatree_src_on_path():
        from teatree.utils.singleton import (  # noqa: PLC0415 — deferred: cold-hook import
            WORKER_SINGLETON,
            flock_is_held,
        )

        return bool(flock_is_held(WORKER_SINGLETON))


def _worker_is_alive() -> bool:
    """True iff a worker owns the loop cadence; fails to False so an unprovable probe never blocks."""
    try:
        return _flock_held()
    except Exception:  # noqa: BLE001 — an unresolvable probe must not fabricate a live worker.
        return False


def _load_core():  # noqa: ANN202 — returns a lazily-imported handle; annotating would pull the type to module scope
    """Import the decision core, or ``None`` on any failure so the caller fails OPEN."""
    try:
        with teatree_src_on_path():
            from teatree.core.gates import cron_loop_shell_gate as core  # noqa: PLC0415 — deferred: cold-hook import
    except Exception:  # noqa: BLE001 — a cold env without teatree fails OPEN, never tracebacks.
        return None
    return core


def _scheduled_text(tool_input: dict) -> str:
    """The scheduled prompt — what the cron/wakeup actually fires."""
    prompt = tool_input.get("prompt", "")
    return prompt if isinstance(prompt, str) else ""


def _escape_reason(core, tool_input: dict) -> str | None:  # noqa: ANN001 — duck-typed core handle
    for field in _TEXT_FIELDS:
        value = tool_input.get(field, "")
        if isinstance(value, str) and (reason := core.cron_loop_ok_reason(value)):
            return reason
    return None


def _finding(core, data: dict) -> "CronLoopShellFinding | None":  # noqa: ANN001 — duck-typed core handle
    """The gate's verdict for this call, or None when it is out of scope or escaped."""
    tool_input = data.get("tool_input", {})
    if not isinstance(tool_input, dict):
        return None
    if reason := _escape_reason(core, tool_input):
        sys.stderr.write(f"NOTE: cron-loop-shell gate skipped via [cron-loop-ok: {reason}].\n")
        return None
    return core.find_cron_loop_shell(_scheduled_text(tool_input), worker_alive=_worker_is_alive())


def handle_block_cron_loop_shell(data: dict) -> bool:
    """Deny a cron/wakeup that shells a ``t3 loop`` command the worker already drives.

    Returns ``True`` (deny emitted) when the call schedules such a command,
    ``False`` (allow) otherwise. Fails open on every resolution failure.
    """
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 deferred back-import

    if data.get("tool_name", "") not in _SCHEDULING_TOOLS or not _cron_loop_shell_gate_enabled():
        return False
    core = _load_core()
    if core is None:
        return False  # cold env without teatree — fail OPEN, never traceback.
    finding = _finding(core, data)
    if finding is None:
        return False
    return _fail_open_or_deny(data, core.deny_reason(finding), gate_id="block-cron-loop-shell")


__all__ = ["handle_block_cron_loop_shell"]
