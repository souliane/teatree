"""Recorded per-invocation user-approval channel for the #777 gate (#953).

``teatree.utils.approval`` holds the pure interactive-TTY primitive
(``require_interactive_approval``) — no dependency on the Django ORM, so it
stays in the ``teatree.utils`` layer. The recorded-approval channel needs
the ``DbApproval``/``DbAudit`` models, so its orchestration lives here in
``teatree.core`` (which legitimately depends on both ``teatree.utils`` and
``teatree.core.models``) rather than forcing a ``utils → core`` dependency
the architecture forbids.

``require_approval`` is the single entry point the ``db refresh`` command
calls. It exposes **two sanctioned channels of the same #777 approval**,
not a gate plus a bypass:

Channel 1 — interactive TTY (delegated to ``require_interactive_approval``,
unchanged): a human at a real terminal types ``yes``.

Channel 2 — recorded per-invocation user approval (``DbApproval`` /
``DbAudit``, #953): a user records an explicit, single-use,
op+tenant-scoped approval; a non-TTY caller may then execute that one op.
The agent can never self-authorize (a maker/coding-agent/loop approver id
is refused at ``DbApproval.record`` time, mirroring the ``MergeClear``
maker≠checker guard), the approval is consumed on use, and a ``DbAudit``
row is written. No valid recorded approval ⇒ fall back to channel 1
unchanged.
"""

from dataclasses import dataclass
from typing import TextIO

from teatree.core.models.db_approval import DbApproval, DbAudit
from teatree.utils.approval import require_interactive_approval


@dataclass(frozen=True, slots=True)
class ApprovalScope:
    """The recorded-approval channel's scope (#953), passed as a unit.

    A single value object so :func:`require_approval` takes one argument
    instead of the irreducible op/tenant/authorizer field list — the
    contract is the dataclass, not a long parameter list (mirrors
    ``teatree.core.models.merge_clear.ClearRequest``).
    """

    op: str
    tenant: str
    user_authorized: str = ""


def require_approval(prompt: str, scope: ApprovalScope, *, stdin: TextIO, stdout: TextIO) -> None:
    """Satisfy the #777 gate via either sanctioned channel, else refuse.

    When ``scope.user_authorized`` is given, look for a valid unconsumed
    :class:`~teatree.core.models.db_approval.DbApproval` scoped to exactly
    this ``scope.op``+``scope.tenant`` (channel 2, #953). If one exists it
    is consumed single-use, a :class:`~teatree.core.models.db_approval.DbAudit`
    row is written, and execution is allowed even with no TTY — the recorded
    user approval *is* the approval. The recorded approver could never be the
    executing agent: a maker/coding-agent/loop id is refused at
    ``DbApproval.record`` time.

    With no ``user_authorized``, or when no valid recorded approval matches
    the requested op+tenant (wrong op/tenant scope, already consumed, or
    none recorded), fall back to the interactive-TTY path
    (:func:`~teatree.utils.approval.require_interactive_approval`) entirely
    unchanged — a human at a terminal can still answer directly. Never
    self-authorizing, per-invocation single-use, scoped strictly to
    op+tenant.
    """
    if scope.user_authorized.strip():
        consumed = DbApproval.consume(scope.op, scope.tenant)
        if consumed is not None:
            DbAudit.objects.create(
                approval=consumed,
                op=consumed.op,
                tenant=consumed.tenant,
                approver_id=consumed.approver_id,
            )
            return

    require_interactive_approval(prompt, stdin=stdin, stdout=stdout)
