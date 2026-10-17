"""Recheck and attest a checkout after a direct owner command or Slack approval."""

import logging
from dataclasses import dataclass
from pathlib import Path

from django.db import DatabaseError, transaction

from teatree.core.cleanup.process_table import read_process_table
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import (
    CardOption,
    QuestionCard,
    counted,
    match_action_option,
    safe_ticket_card_title,
)
from teatree.core.models import Task, Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionAudit, DeferredQuestionError
from teatree.core.models.worktree_occupancy import occupancy_holder
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.worktree_paths import paths_match

logger = logging.getLogger(__name__)


class AttestRefusedError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class CheckoutAttestation:
    worktree: Worktree
    state: CheckoutState
    host_evidence: bool

    @property
    def recorded(self) -> str:
        return self.state.digest if self.host_evidence else f"owner:{self.state.digest}"

    @property
    def action(self) -> ActionRequest:
        return ActionRequest(
            kind=OwnerActionKind.ATTEST,
            target=str(self.worktree.pk),
            binding=self.state.digest,
            host_evidence=self.host_evidence,
        )

    def record(self) -> None:
        self.worktree.merge_extra(set_keys={"attested_state": self.recorded})


def checkout_rows(path: str) -> list[Worktree]:
    return [
        row
        for row in Worktree.objects.exclude(extra__worktree_path__isnull=True).only("extra")
        if row.worktree_path and paths_match(row.worktree_path, path)
    ]


def read_attestation(
    worktree: Worktree, *, expected_digest: str = "", requested_host_evidence: bool | None = None
) -> CheckoutAttestation:
    worktree.refresh_from_db()
    path = worktree.worktree_path
    if len(checkout_rows(path)) != 1 or occupancy_holder(worktree) is not None:
        message = "needs one unoccupied Worktree row to attest."
        raise AttestRefusedError(message, retryable=True)
    table = read_process_table(exclude_ancestors=True)
    if table.usable and table.holds(Path(path)):
        message = f"has live or unverifiable processes: {table.refuse_reason()}"
        raise AttestRefusedError(message, retryable=True)
    try:
        state = checkout_state(Path(path), ticket=worktree.ticket)
        unchanged = checkout_state(Path(path), ticket=worktree.ticket).digest == state.digest
    except (OSError, RuntimeError, ValueError) as exc:
        message = f"state could not be read: {exc}"
        raise AttestRefusedError(message, retryable=True) from exc
    if not unchanged:
        message = "changed while attesting."
        raise AttestRefusedError(message)
    if expected_digest and state.digest != expected_digest:
        message = "changed since the owner was asked; ask again."
        raise AttestRefusedError(message)
    host_evidence = table.usable and requested_host_evidence is not False
    return CheckoutAttestation(worktree, state, host_evidence)


def attest_checkout(worktree: Worktree) -> CheckoutAttestation:
    attestation = read_attestation(worktree)
    attestation.record()
    return attestation


def attest_card(task: Task, attestation: CheckoutAttestation) -> tuple[str, QuestionCard]:
    ticket = task.ticket
    title = safe_ticket_card_title(ticket.short_description, ticket.extra, "this ticket")
    subject = f"<{ticket.issue_url}|{title}>" if ticket.issue_url else title
    state = attestation.state
    process_fact = (
        "Nothing is running in the checkout."
        if attestation.host_evidence
        else "The host process list could not be read."
    )
    holdings = f"{counted(state.commits_ahead, 'commit')} and {counted(state.uncommitted, 'uncommitted file')}"
    why = (
        f"The checkout changed since its last run; it holds {holdings}."
        if attestation.host_evidence
        else f"The host process list is unavailable; accepting permits one run with {holdings}."
    )
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=(
            "The checkout differs from its last recorded state."
            if attestation.host_evidence
            else "The host process list is unavailable.",
            process_fact,
            f"It holds {counted(state.commits_ahead, 'commit')} not on the target branch.",
            f"It has {counted(state.uncommitted, 'uncommitted file')}.",
        ),
        blocker="Only the owner may accept a checkout state the factory did not record.",
        why=why,
        options=(
            CardOption("Accept", "I record this checkout state and resume the waiting task.", recommended=True),
            CardOption("Decline", "The task stays parked until the checkout changes."),
        ),
        action=attestation.action,
    )
    return f"Should I accept the checkout of {subject} as it is now?", card


def ask_owner_to_attest(task: Task, attestation: CheckoutAttestation) -> DeferredQuestion | None:
    question, card = attest_card(task, attestation)
    try:
        marker = action_marker(attestation.action)
        latest = (
            DeferredQuestion.objects.filter(dedupe_marker=marker, answered_at__isnull=False)
            .order_by("-answered_at", "-pk")
            .first()
        )
        if latest is not None and latest.parked_task_id == task.pk:
            option = match_action_option(latest.options_json, latest.answer_text)
            if option is not None and not option.recommended and DeferredQuestionAudit.objects.owner_answer(latest):
                return None
        with transaction.atomic():
            obsolete = (
                DeferredQuestion.owner_pending()
                .filter(
                    evidence__action__kind=OwnerActionKind.ATTEST,
                    evidence__action__target=str(attestation.worktree.pk),
                )
                .exclude(dedupe_marker=marker)
            )
            for row in obsolete.select_for_update():
                row.mark_stale("The checkout changed since this question was asked", resolver_id="checkout_attest")
            return DeferredQuestion.record(question, dedupe_marker=marker, parked_task=task, card=card)
    except (DatabaseError, DeferredQuestionError):
        logger.exception("Could not record checkout attestation question for task %s", task.pk)
        return None
