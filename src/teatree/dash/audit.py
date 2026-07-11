"""One structured audit line per mutating dashboard action (#3162).

Every ``/dash/`` POST — loop pause/resume/disable/enable, availability switch,
gate toggle, ticket FSM transition, debug-session spawn, allowlisted command run
— records exactly one line through :func:`record` before it returns, mirroring
the webhook-hardening posture (``views/_rate_limit.py``) applied to the
outbound-mutation direction. The record is a log emission, not a DB row: it needs
no model and no migration, and it survives in the gunicorn process log the
operator already watches.
"""

import logging

logger = logging.getLogger("teatree.dash.audit")


def record(*, actor: str, action: str, target: str = "", before: str = "", after: str = "") -> None:
    """Emit the one audit line for a mutating action: actor, action, target, before→after."""
    logger.info(
        "dash-action actor=%s action=%s target=%s before=%s after=%s",
        actor or "anonymous",
        action,
        target,
        before,
        after,
    )
