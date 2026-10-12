"""Operator-only correction for a merged row whose content is absent upstream."""

from dataclasses import asdict, dataclass
from typing import NoReturn

from django.db import transaction

from teatree.core.backend_protocols import BackendResolutionError
from teatree.core.forge_content import ContentEvidence, ContentPresence
from teatree.core.merge.ci_rollup import CodeHostQuery
from teatree.core.models import MergeVoid, PullRequest, Ticket
from teatree.core.models.merge_void import ManualVoidPermit
from teatree.core.models.ticket_status import VOID_MERGE_TARGET_STATES
from teatree.utils.forge import forge_from_remote
from teatree.utils.pr_ref import PrRef


class MergeVoidError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class VoidRequest:
    pr_pk: int
    actor: str
    reason: str
    prior_ticket_state: str = ""
    prior_pr_state: str = ""


def _refuse(message: str) -> NoReturn:
    raise MergeVoidError(message)


def _ticket_source(ticket: Ticket, supplied: str) -> str:
    edge = (
        ticket.transitions.filter(to_state=Ticket.State.MERGED)
        .exclude(from_state=Ticket.State.MERGED)
        .order_by("-created_at", "-pk")
        .first()
    )
    recorded = edge.from_state if edge is not None else ""
    if supplied and recorded and supplied != recorded:
        _refuse("Merge void refused: supplied prior ticket state conflicts with the transition ledger")
    source = recorded or supplied
    if source not in VOID_MERGE_TARGET_STATES:
        _refuse("Merge void refused: prior ticket state is unknown or invalid; provide --prior-ticket-state")
    return source


def _pr_source(pr: PullRequest, supplied: str) -> str:
    recorded = pr.state_before_merge
    if supplied and recorded and supplied != recorded:
        _refuse("Merge void refused: supplied prior PR state conflicts with the recorded state")
    source = recorded or supplied
    if source not in {PullRequest.State.OPEN, PullRequest.State.REVIEW_REQUESTED, PullRequest.State.APPROVED}:
        _refuse("Merge void refused: prior PR state is unknown or invalid; provide --prior-pr-state")
    return source


def _checked_evidence(pr_pk: int) -> tuple[PrRef, ContentEvidence]:
    pr = PullRequest.objects.select_related("ticket").filter(pk=pr_pk).first()
    if pr is None:
        _refuse(f"Merge void refused: PR row {pr_pk} does not exist")
    if pr.state != PullRequest.State.MERGED or pr.ticket.state != Ticket.State.MERGED:
        _refuse("Merge void refused: ticket and PR must both be merged")
    if MergeVoid.objects.filter(pull_request=pr).exists():
        _refuse("Merge void refused: this PR was already voided")
    if not pr.iid.isdigit() or not pr.repo.strip():
        _refuse("Merge void refused: PR reference is incomplete")
    ref = PrRef(slug=pr.repo, pr_id=int(pr.iid), host_kind=forge_from_remote(pr.url) or "github")
    try:
        evidence = CodeHostQuery.for_ref(ref).content_evidence()
    except (BackendResolutionError, LookupError, OSError, RuntimeError, ValueError) as exc:
        _refuse(f"Merge void refused: forge content check failed: {exc}")
    if not isinstance(evidence, ContentEvidence) or evidence.presence is not ContentPresence.ABSENT:
        reason_text = evidence.reason if isinstance(evidence, ContentEvidence) else "unreadable forge evidence"
        _refuse(f"Merge void refused: content is present or unverified ({reason_text})")
    return ref, evidence


def _write_void(request: VoidRequest, ref: PrRef, evidence: ContentEvidence) -> tuple[MergeVoid, str, str]:
    permit = ManualVoidPermit(evidence.presence)
    with transaction.atomic():
        locked = PullRequest.objects.select_for_update().select_related("ticket").get(pk=request.pr_pk)
        ticket = Ticket.objects.select_for_update().get(pk=locked.ticket_id)
        if locked.state != PullRequest.State.MERGED or ticket.state != Ticket.State.MERGED:
            _refuse("Merge void refused: merged state changed during the forge check")
        if MergeVoid.objects.filter(pull_request=locked).exists():
            _refuse("Merge void refused: this PR was already voided")
        ticket_source = _ticket_source(ticket, request.prior_ticket_state)
        pr_source = _pr_source(locked, request.prior_pr_state)
        audit = MergeVoid.objects.create(
            ticket=ticket,
            pull_request=locked,
            actor=request.actor,
            reason=request.reason,
            ticket_state_before_merge=ticket_source,
            pr_state_before_merge=pr_source,
            head_sha=evidence.head_sha,
            evidence=asdict(evidence),
        )
        recorded = (ticket.extra or {}).get("forge_merge_evidence")
        if isinstance(recorded, dict) and (
            str(recorded.get("slug", "")).casefold() == ref.slug.casefold()
            and recorded.get("pr_id") == ref.pr_id
            and str(recorded.get("host_kind") or ref.host_kind) == ref.host_kind
        ):
            ticket.extra = {key: value for key, value in ticket.extra.items() if key != "forge_merge_evidence"}
        ticket.void_merge(ticket_source, permit=permit)
        locked.void_merge(pr_source, permit=permit)
        ticket.save()
        locked.save()
    return audit, ticket_source, pr_source


def void_merge(
    *, pr_pk: int, actor: str, reason: str, prior_ticket_state: str = "", prior_pr_state: str = ""
) -> MergeVoid:
    actor = actor.strip()
    reason = reason.strip()
    if not actor:
        _refuse("Merge void refused: actor is required")
    if not reason:
        _refuse("Merge void refused: reason is required")
    request = VoidRequest(pr_pk, actor, reason, prior_ticket_state, prior_pr_state)
    ref, evidence = _checked_evidence(pr_pk)
    audit, ticket_source, pr_source = _write_void(request, ref, evidence)
    if not MergeVoid.objects.filter(pk=audit.pk, ticket__state=ticket_source, pull_request__state=pr_source).exists():
        _refuse("Merge void was written but could not be verified by re-read")
    return audit
