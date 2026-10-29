"""Approval publishing through the ticket requester's OAuth grant."""

from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from teatree.cli.review.approval import identity_has_reviewed
from teatree.cli.review.audit import ReviewArtifactNotVerifiedError
from teatree.cli.review.post_impl import approve_impl, unapprove_impl
from teatree.core.models import OnBehalfApproval, OnBehalfAudit

if TYPE_CHECKING:
    from teatree.cli.review.service import ReviewService


def _reviewed(service: "ReviewService", repo: str, mr: int) -> tuple[str, int] | None:
    encoded = repo.replace("/", "%2F")
    reviewed, error = identity_has_reviewed(service.api, encoded, mr)
    if error:
        return error, 1
    if not reviewed:
        return (
            f"Refusing to approve !{mr}: review before approve — no review note authored by your "
            "identity exists on this MR yet. Post a review (`t3 review post-comment` / "
            "`post-comment`) first, then approve."
        ), 1
    return None


def _approval_preflight(
    service: "ReviewService", repo: str, mr: int, action: str, approver_key: str
) -> OnBehalfApproval | tuple[str, int]:
    target = f"{repo}!{mr}"
    if not service.oauth:
        return "OAuth grant required for operator approval", 1
    approval = (
        OnBehalfApproval.objects.filter(
            target=target,
            action=action,
            approver_id=approver_key,
            consumed_at__isnull=True,
        )
        .order_by("created_at")
        .first()
    )
    if approval is None:
        return f"No consent from {approver_key} for {action} on {target}", 1
    actual = service.api.current_username()
    if f"gitlab:{actual.casefold()}" != approver_key:
        return f"OAuth grant identity does not match requester {approver_key}", 1
    if action == "approve":
        refusal = _reviewed(service, repo, mr)
        if refusal:
            return refusal
    return approval


def publish_operator_approval(
    service: "ReviewService", repo: str, mr: int, action: str, approver_key: str
) -> tuple[str, int]:
    target = f"{repo}!{mr}"
    encoded = repo.replace("/", "%2F")
    preflight = _approval_preflight(service, repo, mr, action, approver_key)
    if isinstance(preflight, tuple):
        return preflight
    approval = preflight
    claimed_at = timezone.now()
    with transaction.atomic():
        claimed = OnBehalfApproval.objects.filter(pk=approval.pk, consumed_at__isnull=True).update(
            consumed_at=claimed_at
        )
    if not claimed:
        return f"No consent from {approver_key} for {action} on {target}", 1
    try:
        result = (
            approve_impl(service, repo, mr, encoded=encoded)
            if action == "approve"
            else unapprove_impl(service, repo, mr, encoded=encoded)
        )
    except ReviewArtifactNotVerifiedError as error:
        OnBehalfApproval.objects.filter(pk=approval.pk, consumed_at=claimed_at).update(consumed_at=None)
        return str(error), 1
    except Exception:
        OnBehalfApproval.objects.filter(pk=approval.pk, consumed_at=claimed_at).update(consumed_at=None)
        raise
    if result[1]:
        OnBehalfApproval.objects.filter(pk=approval.pk, consumed_at=claimed_at).update(consumed_at=None)
        return result
    with transaction.atomic():
        OnBehalfAudit.objects.create(approval=approval, target=target, action=action, approver_id=approver_key)
    return result
