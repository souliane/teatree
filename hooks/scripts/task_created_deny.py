"""The ``TaskCreated`` deny surface — the reason the caller actually reads (#4216).

Split out of ``hook_router.py`` by concern (module health; the router is
shrink-only). All three ``TaskCreated`` gates deny through this one emitter.

The harness's task-creation consumer reads exactly ONE field of a hook result:
the ``blockingError`` its runner derives. ``{"continue": false, "stopReason":
…}`` — the teammate-stop envelope — becomes ``preventContinuation``, which that
consumer never looks at, so a deny carrying only those two keys fell through to
the exit-2 fallback ``[<command>]: <stderr or "No stderr output">``. With the
router's stdout-only deny that surfaced as an EMPTY failure: a block with no
reason, no remedy, and nothing to distinguish it from a crashed hook. A blocking
gate whose remedy is invisible cannot be complied with, only worked around.

``decision: "block"`` is the one stdout key the runner turns into a
``blockingError`` carrying our own text, so the deny now emits it alongside the
stop envelope (which stays, for the consumers that DO read it). Stderr is the
runner's other channel at exit 2; it is modelled here but the DENY never writes
it, because a ``TaskCreated`` handler's stderr risks aborting the event on its
own — so the payload alone has to carry the reason.

``harness_surfaced_deny_text`` models that read so the emitter can enforce it:
a reason that would NOT reach the caller fails OPEN and logs (on the allow path,
where the documented contract shows neither stream), rather than blocking
silently.

A bare sibling module (like ``subagent_skill_gate`` / ``django_bootstrap``): the
router puts its own dir on ``sys.path``, so the import resolves both as the live
hook and when imported as ``hooks.scripts.hook_router`` in tests. It never
imports the router back.
"""

import json
import sys
from collections.abc import Mapping
from typing import TypedDict

# Functional form because ``continue`` is a Python keyword — the harness names the
# key, so it cannot be spelled as a class attribute.
TaskCreatedDeny = TypedDict(
    "TaskCreatedDeny",
    {"continue": bool, "stopReason": str, "decision": str, "reason": str},
)

# The documented ``TaskCreated`` contract: exit 0 shows nothing, exit 2 shows the
# stderr fallback to the model and prevents the task creation.
DENY_EXIT_CODE = 2

# What the runner substitutes for a ``decision: "block"`` whose ``reason`` is blank.
_HARNESS_DEFAULT_BLOCK_REASON = "Blocked by hook"


def build_deny_payload(reason: str) -> TaskCreatedDeny:
    """The ``TaskCreated`` deny envelope: the stop keys plus the one the caller reads."""
    text = reason.strip()
    return {"continue": False, "stopReason": text, "decision": "block", "reason": text}


def harness_surfaced_deny_text(payload: Mapping[str, object], stderr: str, *, exit_code: int) -> str:
    """What the harness shows the caller for a ``TaskCreated`` deny.

    ``continue``/``stopReason`` are deliberately absent from this model: they set
    ``preventContinuation``, which the task-creation consumer never reads.
    """
    if payload.get("decision") == "block":
        return str(payload.get("reason") or _HARNESS_DEFAULT_BLOCK_REASON)
    if exit_code >= DENY_EXIT_CODE:
        return stderr.strip()
    return ""


def emit_task_create_deny(reason: str) -> bool:
    """Emit the ``TaskCreated`` deny envelope and return ``True``.

    ``main`` translates the ``True`` return into ``sys.exit(2)``, the documented
    block signal. Fails OPEN (returns ``False``) and logs when *reason* would not
    reach the caller verbatim — an unreadable deny is worse than no deny.
    """
    text = reason.strip()
    payload = build_deny_payload(text)
    # The stderr channel is empty because this emitter never writes it — so the
    # payload alone has to carry the reason, which is exactly what is checked.
    if not text or harness_surfaced_deny_text(payload, "", exit_code=DENY_EXIT_CODE) != text:
        sys.stderr.write(
            f"NOTE: TaskCreated deny suppressed — its reason would not reach the caller: {reason!r}\n",
        )
        return False
    json.dump(payload, sys.stdout)
    return True
