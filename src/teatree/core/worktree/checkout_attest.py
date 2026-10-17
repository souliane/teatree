"""Recheck and attest a checkout after a direct owner command or Slack approval."""

import logging
from dataclasses import dataclass
from pathlib import Path

from django.db import DatabaseError, transaction

from teatree.core.cleanup.process_table import read_process_table
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.phases import normalize_phase
from teatree.core.modelkit.question_card import CardOption, QuestionCard, counted, safe_ticket_card_title
from teatree.core.models import PullRequest, Task, Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError
from teatree.core.models.question_actions import owner_declined
from teatree.core.models.worktree_occupancy import occupancy_holder
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.worktree_paths import paths_match

logger = logging.getLogger(__name__)

_STEPS: dict[str, tuple[str, str]] = {
    "coding": ("coding", "write the change"),
    "testing": ("testing", "run and fix the tests"),
    "shipping": ("shipping", "push the work for review"),
    "debugging": ("debugging", "fix a failing check"),
    "e2e": ("end-to-end", "test the app end to end"),
    "retro": ("wrap-up", "note lessons from the run"),
    "architectural_review": ("design review", "fix design problems in the code"),
}
_NEXT_STEP = ("next", "continue the work")


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
        message = "has live or unverifiable processes."
        raise AttestRefusedError(message, retryable=True)
    if requested_host_evidence and (not table.usable or table.eviction_gaps):
        message = f"cannot rule out live processes: {table.eviction_refuse_reason()}"
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
    host_evidence = table.usable and not table.eviction_gaps and requested_host_evidence is not False
    return CheckoutAttestation(worktree, state, host_evidence)


def attest_checkout(worktree: Worktree) -> CheckoutAttestation:
    attestation = read_attestation(worktree)
    attestation.record()
    return attestation


def _options(attestation: CheckoutAttestation, *, blocks_pull_request: bool) -> tuple[CardOption, CardOption]:
    accept = (
        "The step resumes on the folder as it is; changes the factory did not make become part of its work."
        if attestation.host_evidence
        else "The step runs once on the folder as it is; "
        "edits the factory did not make, even unfinished ones, become part of its work."
    )
    decline = (
        "The step stays stuck until the folder changes, and its pull request keeps waiting."
        if blocks_pull_request
        else "The step stays stuck until the folder changes; no pull request waits on it."
    )
    return CardOption("Accept", accept, recommended=True), CardOption("Decline", decline)


def attest_card(task: Task, attestation: CheckoutAttestation) -> tuple[str, QuestionCard]:
    ticket = task.ticket
    title = safe_ticket_card_title(ticket.short_description, ticket.extra, "this ticket")
    subject = f"<{ticket.issue_url}|{title}>" if ticket.issue_url else title
    step, work = _STEPS.get(normalize_phase(task.phase), _NEXT_STEP)
    pull_request = PullRequest.objects.live().filter(ticket=ticket).order_by("pk").values_list("url", flat=True).first()
    waits = f"<{pull_request}|its pull request> waits on it" if pull_request else "it blocks no pull request"
    state = attestation.state
    process_fact = (
        "Nothing is running in the checkout."
        if attestation.host_evidence
        else "The host process list could not rule out live processes."
    )
    holdings = f"{counted(state.commits_ahead, 'commit')} and {counted(state.uncommitted, 'uncommitted file')}"
    doubt = (
        f"nothing is running in its folder, but the folder changed since its last run and holds {holdings}"
        if attestation.host_evidence
        else f"its folder holds {holdings} and the factory cannot prove no program is still editing it"
    )
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=(
            "The checkout differs from its last recorded state."
            if attestation.host_evidence
            else "The host process list could not rule out live processes.",
            process_fact,
            f"It holds {counted(state.commits_ahead, 'commit')} not on the target branch.",
            f"It has {counted(state.uncommitted, 'uncommitted file')}.",
        ),
        blocker="Only the owner may accept a checkout state the factory did not record.",
        why=f"It will {work}; {waits}; {doubt}.",
        options=_options(attestation, blocks_pull_request=bool(pull_request)),
        action=attestation.action,
    )
    return f"Should the {step} step of {subject} run on its working folder as it is now?", card


def ask_owner_to_attest(task: Task, attestation: CheckoutAttestation) -> DeferredQuestion | None:
    question, card = attest_card(task, attestation)
    try:
        marker = action_marker(attestation.action)
        latest = (
            DeferredQuestion.objects.filter(dedupe_marker=marker, answered_at__isnull=False)
            .order_by("-answered_at", "-pk")
            .first()
        )
        if latest is not None and latest.parked_task_id == task.pk and owner_declined(latest):
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
