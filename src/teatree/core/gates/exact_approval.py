"""Single-use recorded approval bound to an exact outbound mutation."""

import contextlib
import hashlib
import json
import shlex
from collections.abc import Iterator
from typing import TYPE_CHECKING, cast

from django.apps import apps

if TYPE_CHECKING:
    from teatree.core.models import OnBehalfApproval, OnBehalfAudit


class ExactApprovalMissingError(ValueError):
    pass


class ExactApprovalStaleError(ValueError):
    pass


def write_target(namespace: str, object_path: str, *bound: str | int | None) -> str:
    return f"{namespace}:{object_path}:{hashlib.sha256(json.dumps(bound).encode()).hexdigest()}"


def approval_hint(target: str, action: str) -> dict[str, str]:
    return {
        "action": action,
        "target": target,
        "record_command": f"t3 review approve-on-behalf {shlex.quote(target)} {action} --approver <owner-id>",
    }


@contextlib.contextmanager
def spent_approval(target: str, action: str) -> Iterator[None]:
    """Keep the claim and audit outside the outbound request's potentially long transaction."""
    approval = _claim(target, action)
    try:
        yield
    finally:
        _audit(approval)


def _claim(target: str, action: str) -> "OnBehalfApproval":
    approval_model = cast("type[OnBehalfApproval]", apps.get_model("core", "OnBehalfApproval"))

    if (approval := approval_model.consume(target, action)) is not None:
        return approval
    scope = target.rpartition(":")[0]
    stale = approval_model.objects.filter(action=action, consumed_at__isnull=True, target__startswith=f"{scope}:")
    if stale.exists():
        msg = (
            f"an approval is recorded for {scope}, but for different text or arguments than this call would "
            "write. Re-run the dry run and approve the new target. Nothing was written."
        )
        raise ExactApprovalStaleError(msg)
    msg = (
        f"no recorded approval covers this write ({action} on {target}). Show the owner the dry run and have them "
        f"record it with `{approval_hint(target, action)['record_command']}`. Nothing was written."
    )
    raise ExactApprovalMissingError(msg)


def _audit(approval: "OnBehalfApproval") -> None:
    audit_model = cast("type[OnBehalfAudit]", apps.get_model("core", "OnBehalfAudit"))

    audit_model.objects.create(
        approval=approval, target=approval.target, action=approval.action, approver_id=approval.approver_id
    )
