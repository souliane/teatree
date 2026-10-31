"""Approval publishing through the ticket requester's OAuth grant."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from django.db import transaction
from django.utils import timezone

from teatree.cli.review.approval import identity_has_reviewed
from teatree.cli.review.audit import ReviewArtifactNotVerifiedError
from teatree.cli.review.post_impl import approve_impl, unapprove_impl
from teatree.core.models import OnBehalfApproval, OnBehalfAudit, OperatorIdentity
from teatree.core.models.operator_identity import canonical_operator_key
from teatree.utils.forge import FORGES

if TYPE_CHECKING:
    from teatree.cli.review.service import ReviewService


@dataclass(frozen=True)
class OperatorApprover:
    consent_key: str
    forge_key: str


def _same_operator(approver: OperatorApprover) -> bool:
    if approver.consent_key == approver.forge_key:
        return True
    try:
        consent_platform, consent_handle = canonical_operator_key(approver.consent_key).split(":", 1)
        forge_platform, forge_handle = canonical_operator_key(approver.forge_key).split(":", 1)
    except ValueError:
        return False
    if forge_platform not in FORGES:
        return False
    consent_user = (
        OperatorIdentity.objects.filter(
            platform=consent_platform, handle=consent_handle, user__is_active=True, user__is_superuser=True
        )
        .values_list("user_id", flat=True)
        .first()
    )
    forge_user = (
        OperatorIdentity.objects.filter(
            platform=forge_platform, handle=forge_handle, user__is_active=True, user__is_superuser=True
        )
        .values_list("user_id", flat=True)
        .first()
    )
    return consent_user is not None and consent_user == forge_user


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


def pending_consent(target: str, action: str, consent_key: str) -> OnBehalfApproval | None:
    return (
        OnBehalfApproval.objects.filter(target=target, action=action, approver_id=consent_key, consumed_at__isnull=True)
        .order_by("created_at")
        .first()
    )


def identity_refusal(approver: OperatorApprover, actual_key: str) -> str:
    if actual_key == approver.forge_key or _same_operator(OperatorApprover(approver.consent_key, actual_key)):
        return ""
    return f"OAuth grant identity does not match requester {approver.consent_key}"


def _approval_preflight(
    service: "ReviewService", repo: str, mr: int, action: str, approver: OperatorApprover
) -> OnBehalfApproval | tuple[str, int]:
    target = f"{repo}!{mr}"
    if not service.oauth:
        return "OAuth grant required for operator approval", 1
    if not _same_operator(approver):
        return "operator approval identities belong to different users", 1
    approval = pending_consent(target, action, approver.consent_key)
    if approval is None:
        return f"No consent from {approver.consent_key} for {action} on {target}", 1
    if refusal := identity_refusal(approver, f"gitlab:{service.api.current_username().casefold()}"):
        return refusal, 1
    if action == "approve":
        refusal = _reviewed(service, repo, mr)
        if refusal:
            return refusal
    return approval


def claim_publish_audit(approval: OnBehalfApproval, write: Callable[[], tuple[str, int]]) -> tuple[str, int]:
    claimed_at = timezone.now()
    with transaction.atomic():
        claimed = OnBehalfApproval.objects.filter(pk=approval.pk, consumed_at__isnull=True).update(
            consumed_at=claimed_at
        )
    if not claimed:
        return f"No consent from {approval.approver_id} for {approval.action} on {approval.target}", 1
    claim = OnBehalfApproval.objects.filter(pk=approval.pk, consumed_at=claimed_at)
    try:
        result = write()
    except ReviewArtifactNotVerifiedError as error:
        claim.update(consumed_at=None)
        return str(error), 1
    except Exception:
        claim.update(consumed_at=None)
        raise
    if result[1]:
        claim.update(consumed_at=None)
        return result
    with transaction.atomic():
        OnBehalfAudit.objects.create(
            approval=approval, target=approval.target, action=approval.action, approver_id=approval.approver_id
        )
    return result


def publish_operator_approval(
    service: "ReviewService", repo: str, mr: int, action: str, approver_key: str | OperatorApprover
) -> tuple[str, int]:
    encoded = repo.replace("/", "%2F")
    approver = (
        approver_key if isinstance(approver_key, OperatorApprover) else OperatorApprover(approver_key, approver_key)
    )
    preflight = _approval_preflight(service, repo, mr, action, approver)
    if isinstance(preflight, tuple):
        return preflight
    write = approve_impl if action == "approve" else unapprove_impl
    return claim_publish_audit(preflight, lambda: write(service, repo, mr, encoded=encoded))
