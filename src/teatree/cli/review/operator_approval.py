"""Approval publishing through the ticket requester's OAuth grant."""

# ruff: noqa: SLF001 — approval helpers implement ReviewService operations.

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
    reviewed, error = identity_has_reviewed(service._get_api(), encoded, mr)
    if error:
        return error, 1
    if not reviewed:
        return (
            f"Refusing to approve !{mr}: review before approve — no review note authored by your "
            "identity exists on this MR yet. Post a review (`t3 review post-comment` / "
            "`post-comment`) first, then approve."
        ), 1
    return None


def publish_operator_approval(
    service: "ReviewService", repo: str, mr: int, action: str, approver_key: str
) -> tuple[str, int]:
    target = f"{repo}!{mr}"
    encoded = repo.replace("/", "%2F")
    try:
        with transaction.atomic():
            approval = (
                OnBehalfApproval.objects.select_for_update()
                .filter(target=target, action=action, approver_id=approver_key, consumed_at__isnull=True)
                .order_by("created_at")
                .first()
            )
            if approval is None or not service.oauth:
                reason = (
                    "OAuth grant required for operator approval"
                    if not service.oauth
                    else f"No consent from {approver_key} for {action} on {target}"
                )
                return reason, 1
            actual = service._get_api().current_username()
            if f"gitlab:{actual.casefold()}" != approver_key:
                return f"OAuth grant identity does not match requester {approver_key}", 1
            if action == "approve":
                refusal = _reviewed(service, repo, mr)
                if refusal:
                    return refusal
                result = approve_impl(service, repo, mr, encoded=encoded)
            else:
                result = unapprove_impl(service, repo, mr, encoded=encoded)
            if result[1]:
                transaction.set_rollback(True)
                return result
            approval.consumed_at = timezone.now()
            approval.save(update_fields=["consumed_at"])
            OnBehalfAudit.objects.create(approval=approval, target=target, action=action, approver_id=approver_key)
            return result
    except ReviewArtifactNotVerifiedError as error:
        return str(error), 1
