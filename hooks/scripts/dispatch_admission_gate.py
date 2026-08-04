"""Consult the admission governor on an interactive ``Agent``/``Task`` dispatch (#4107).

``decide_admission`` is described as "one chokepoint every dispatcher asks" and
had exactly two callers, both factory lanes. Nothing on the INTERACTIVE path
asked: an orchestrator session dispatching through the harness ``Agent``/``Task``
tool was admitted with no ceiling, no load brake and no per-agent test-worker
budget, so the governor governed the headless population while the agent
population on the box is the SUM of both. Measured at load 58 on 8 cores with
1 GB free while the factory's own ``issue_implementer_max_concurrent = 3`` held.

The dispatch interception point already existed — this module supplies the
admission DECISION on it, in both arms, because the ``Task``/``Workflow`` fan-out
bypasses ``PreToolUse`` and only ``TaskCreated`` reaches it.

The verdict itself lives in :mod:`teatree.core.dispatch_admission`, so all three
lanes route through the one pure decision function and can never diverge. The
kill-switch and rollback lever is the EXISTING ``admission_governor_enabled``
setting (``t3 <overlay> config_setting set admission_governor_enabled false``) —
deliberately no second flag, which would let the two drift.

NEVER-LOCKOUT: the ``PreToolUse`` deny routes through the router's shared
``_fail_open_or_deny`` chokepoint (back-imported lazily), so the always-allowed
self-rescue commands and the master ``danger_gate_fail_open`` switch relax it;
a per-call ``[admission-ok: <reason>]`` token in the dispatch prompt / task
fields allows one call (an empty reason does not); and ANY internal error,
an unbootstrappable Django and an unimportable ``teatree`` all fail OPEN. The
``TaskCreated`` arm additionally swallows its own exceptions and emits no
stderr — the harness ABORTS ``TaskCreated`` on any handler stderr, so
``main()``'s traceback-writing swallow is a lockout there, not a backstop.

Cold-import safe: the module top imports only stdlib plus the already-extracted
``orchestration_boundary_signals`` sibling — never Django / ``teatree.core``.
"""

import contextlib
import io
import logging
import re
import sys
from collections.abc import Iterator

from hooks.scripts.orchestration_boundary_signals import call_is_from_subagent

# Alias the bare and ``hooks.scripts.`` identities so the handlers the router
# re-exports and a test patching a helper here operate on ONE module object.
sys.modules.setdefault("dispatch_admission_gate", sys.modules[__name__])
sys.modules.setdefault("hooks.scripts.dispatch_admission_gate", sys.modules[__name__])

#: The two harness tool names that spawn a sub-agent.
DISPATCH_TOOLS = frozenset({"Agent", "Task"})

# Per-call opt-out, mirroring ``[fg-ok: <reason>]`` / ``[quote-ok: <reason>]``.
# An empty reason does not unblock.
_ADMISSION_OK_RE = re.compile(r"\[admission-ok:\s*\S[^\]]*?\s*\]")

#: Scanned prefix of the dispatch payload — a token buried deep in a long brief
#: must not silently authorise the whole prompt (the ``[quote-ok:]`` precedent).
_TOKEN_SCAN_CHARS = 512


def _block_message(reason: str) -> str:
    return (
        "[admission-governor] Dispatch DENIED — the governor is braking: "
        f"{reason}.\n"
        "An interactive dispatch adds to the SAME box the factory lanes run on, so it is "
        "governed by the same governor. Wait for the brake to release "
        "(the load watermark has hysteresis, so it re-admits under the lower one), add an "
        "explicit `[admission-ok: <reason>]` marker to the dispatch for a genuine "
        "must-run-now dispatch, or disable the governor entirely with "
        "`t3 <overlay> config_setting set admission_governor_enabled false`."
    )


def _denied_reason(*, apply_ceiling: bool) -> str | None:
    """The governor's DENY reason for one more dispatch, or ``None`` to admit.

    Fails OPEN (``None``) when ``teatree`` / Django cannot be bootstrapped — the
    live hook is a bare ``python3`` subprocess with no such guarantee (#1314).
    """
    from hooks.scripts.django_bootstrap import bootstrap_teatree_django  # noqa: PLC0415 — deferred: cold-hook import

    if not bootstrap_teatree_django():
        return None
    from teatree.core.dispatch_admission import dispatch_admission_denied_reason  # noqa: PLC0415 — deferred: post-setup

    return dispatch_admission_denied_reason(apply_ceiling=apply_ceiling)


def handle_dispatch_admission(data: dict) -> bool:
    """Deny an ``Agent``/``Task`` dispatch the admission governor refuses (#4107).

    A sub-agent's onward dispatch skips the CEILING — its own lane already
    admitted it, and its own claim is counted in that ceiling, so re-clamping it
    would deadlock it against itself. The BRAKES still apply there: box
    saturation is real whoever dispatched.
    """
    try:
        if data.get("tool_name") not in DISPATCH_TOOLS:
            return False
        tool_input = data.get("tool_input") or {}
        prompt = tool_input.get("prompt", "") if isinstance(tool_input, dict) else ""
        if isinstance(prompt, str) and _ADMISSION_OK_RE.search(prompt[:_TOKEN_SCAN_CHARS]):
            return False
        reason = _denied_reason(apply_ceiling=not call_is_from_subagent(data))
    except Exception:  # noqa: BLE001 — crash-proof hook: a gate bug must never wedge the dispatch
        return False
    if reason is None:
        return False
    from hooks.scripts.hook_router import _fail_open_or_deny  # noqa: PLC0415 — deferred back-import: avoids a cycle

    return _fail_open_or_deny(data, _block_message(reason), gate_id="dispatch_admission")


@contextlib.contextmanager
def _muzzled() -> Iterator[None]:
    """Suppress every stderr channel for the block — ``TaskCreated`` aborts on any.

    Both channels are stopped because a probe failure inside the governor logs
    through ``logging``, whose handlers captured the ORIGINAL ``sys.stderr`` at
    construction time and so survive ``redirect_stderr`` alone.
    """
    logging.disable(logging.CRITICAL)
    try:
        with contextlib.redirect_stderr(io.StringIO()):
            yield
    finally:
        logging.disable(logging.NOTSET)


def handle_dispatch_admission_on_task_create(data: dict) -> bool:
    """Deny a fanned-out ``Task`` the admission governor refuses — the ``TaskCreated`` arm.

    The ``Task``/``Workflow`` fan-out bypasses ``PreToolUse``, so the arm above
    never fires on it. The ``TaskCreated`` schema carries no ``agent_id``, so the
    dispatch's origin is unknowable and the CEILING is never applied here —
    unknown must not brake on a count it cannot attribute. The quota and load
    brakes are origin-independent and do apply.
    """
    try:
        with _muzzled():
            if not data.get("session_id"):
                return False
            payload = f"{data.get('task_subject', '') or ''}\n{data.get('task_description', '') or ''}"
            if _ADMISSION_OK_RE.search(payload[:_TOKEN_SCAN_CHARS]):
                return False
            reason = _denied_reason(apply_ceiling=False)
            if reason is None:
                return False
            from hooks.scripts.hook_router import emit_task_create_deny  # noqa: PLC0415 — deferred back-import

            return emit_task_create_deny(_block_message(reason))
    except Exception:  # noqa: BLE001 — crash-proof hook: a TaskCreated traceback on stderr is a LOCKOUT
        return False
