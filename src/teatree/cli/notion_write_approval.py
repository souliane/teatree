"""The recorded approval a Notion write on the MCP surface spends.

The row is ``OnBehalfApproval``, recorded by ``t3 review approve-on-behalf``. The gate is NOT
``require_on_behalf_approval``: that one lets a write through with no approval under a
permitting posture, an ``on_behalf_auto_actions`` entry or the graduated dial. Here the approval
is unconditional and is bound to a digest of the exact text the write will land.
"""

import contextlib
import hashlib
import json
import shlex
from collections.abc import Iterator
from typing import TYPE_CHECKING

from teatree.backends.notion.errors import NotionBlockChangedError, NotionWriteNotApprovedError

if TYPE_CHECKING:
    from teatree.core.models import OnBehalfApproval


def write_target(object_path: str, *bound: str | int | None) -> str:
    """The approval scope: the object written, and a full SHA-256 of everything the approval binds."""
    return f"notion:{object_path}:{hashlib.sha256(json.dumps(bound).encode()).hexdigest()}"


def approval_hint(target: str, action: str) -> dict[str, str]:
    return {
        "action": action,
        "target": target,
        "record_command": f"t3 review approve-on-behalf {shlex.quote(target)} {action} --approver <owner-id>",
    }


@contextlib.contextmanager
def spent_approval(target: str, action: str) -> Iterator[None]:
    """Spend the recorded approval before the write runs, and audit it whether or not the write succeeds.

    The claim is its own short transaction and the audit runs after the send: one ``atomic`` block around
    the send would hold SQLite's write lock across the page re-walk's HTTP calls, and a raise would roll
    back the ledger row that records a write whose outcome is unknown.
    """
    approval = _claim(target, action)
    try:
        yield
    finally:
        _audit(approval)


def _claim(target: str, action: str) -> "OnBehalfApproval":
    from teatree.core.models import OnBehalfApproval  # noqa: PLC0415 — deferred: ORM needs the app registry

    if (approval := OnBehalfApproval.consume(target, action)) is not None:
        return approval
    scope = target.rpartition(":")[0]
    stale = OnBehalfApproval.objects.filter(action=action, consumed_at__isnull=True, target__startswith=f"{scope}:")
    if stale.exists():
        msg = (
            f"an approval is recorded for {scope}, but for different text, or another overlay, than this call would "
            "write — the page, the arguments or the overlay changed since the dry run. Re-run it with dry_run=true "
            "and approve the new target. Nothing was written."
        )
        raise NotionBlockChangedError(msg)
    msg = (
        f"no recorded approval covers this write ({action} on {target}). Show the owner the dry run, have them "
        f"record it with `{approval_hint(target, action)['record_command']}`, then call again. Nothing was written."
    )
    raise NotionWriteNotApprovedError(msg)


def _audit(approval: "OnBehalfApproval") -> None:
    from teatree.core.models import OnBehalfAudit  # noqa: PLC0415 — deferred: ORM needs the app registry

    OnBehalfAudit.objects.create(
        approval=approval, target=approval.target, action=approval.action, approver_id=approval.approver_id
    )
