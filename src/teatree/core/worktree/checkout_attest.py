"""Recheck and attest a checkout after a direct owner command or Slack approval."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from teatree.core.cleanup.process_table import ProcessTable, read_process_table
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, safe_card_title
from teatree.core.models import Task, Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError
from teatree.core.models.worktree_occupancy import occupancy_holder
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.worktree_paths import paths_match

logger = logging.getLogger(__name__)


class AttestRefusedError(RuntimeError):
    pass


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
        return ActionRequest(kind=OwnerActionKind.ATTEST, target=str(self.worktree.pk), binding=self.state.digest)


def checkout_rows(path: str) -> list[Worktree]:
    return [
        row
        for row in Worktree.objects.exclude(extra__worktree_path__isnull=True).only("extra")
        if row.worktree_path and paths_match(row.worktree_path, path)
    ]


def read_attestation(
    worktree: Worktree,
    *,
    table_reader: Callable[..., ProcessTable] | None = None,
    state_reader: Callable[..., CheckoutState] | None = None,
) -> CheckoutAttestation:
    table_reader = table_reader or read_process_table
    state_reader = state_reader or checkout_state
    path = worktree.worktree_path
    if len(checkout_rows(path)) != 1 or occupancy_holder(worktree) is not None:
        message = "needs one unoccupied Worktree row to attest."
        raise AttestRefusedError(message)
    table = table_reader(exclude_ancestors=True)
    if table.usable and table.holds(Path(path)):
        message = f"has live or unverifiable processes: {table.refuse_reason()}"
        raise AttestRefusedError(message)
    try:
        state = state_reader(Path(path), ticket=worktree.ticket)
        if state_reader(Path(path), ticket=worktree.ticket).digest != state.digest:
            message = "changed while attesting."
            raise AttestRefusedError(message)
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, AttestRefusedError):
            raise
        message = f"state could not be read: {exc}"
        raise AttestRefusedError(message) from exc
    return CheckoutAttestation(worktree, state, table.usable)


def attest_checkout(
    worktree: Worktree,
    *,
    expected_digest: str = "",
    table_reader: Callable[..., ProcessTable] | None = None,
    state_reader: Callable[..., CheckoutState] | None = None,
) -> CheckoutAttestation:
    attestation = read_attestation(worktree, table_reader=table_reader, state_reader=state_reader)
    if expected_digest and attestation.state.digest != expected_digest:
        message = "changed since the owner was asked; ask again."
        raise AttestRefusedError(message)
    worktree.merge_extra(set_keys={"attested_state": attestation.recorded})
    return attestation


def attest_card(task: Task, attestation: CheckoutAttestation) -> tuple[str, QuestionCard]:
    ticket = task.ticket
    title = safe_card_title(str(ticket.short_description or ticket.extra.get("issue_title", "")), "this ticket")
    subject = f"<{ticket.issue_url}|{title}>" if ticket.issue_url else title
    state = attestation.state
    process_fact = (
        "Nothing is running in the checkout."
        if attestation.host_evidence
        else "The host process list could not be read."
    )
    why = (
        f"The checkout changed since its last run; it holds {state.commits_ahead} commits and "
        f"{state.uncommitted} uncommitted files."
        if attestation.host_evidence
        else f"The host process list is unavailable; accepting permits one run with {state.commits_ahead} commits "
        f"and {state.uncommitted} uncommitted files."
    )
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=(
            "The checkout differs from its last recorded state."
            if attestation.host_evidence
            else "The host process list is unavailable.",
            process_fact,
            f"It holds {state.commits_ahead} commits not on the target branch.",
            f"It has {state.uncommitted} uncommitted files.",
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
        return DeferredQuestion.record(
            question,
            dedupe_marker=action_marker(attestation.action),
            parked_task=task,
            card=card,
        )
    except DeferredQuestionError:
        logger.exception("Could not record checkout attestation question for task %s", task.pk)
        return None
