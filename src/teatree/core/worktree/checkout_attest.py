"""Recheck and attest a checkout after a direct owner command or Slack approval."""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

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
from teatree.core.worktree.branch_owner import worktree_repo_slug
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.worktree_paths import paths_match
from teatree.utils.url_slug import pr_ref_from_url

logger = logging.getLogger(__name__)


class _Step(NamedTuple):
    label: str
    work: str
    may_block_pull_request: bool


_STEPS: dict[str, _Step] = {
    "coding": _Step("coding", "write the change", may_block_pull_request=True),
    "testing": _Step("testing", "run and fix the tests", may_block_pull_request=True),
    "shipping": _Step("shipping", "push the work for review", may_block_pull_request=True),
    "debugging": _Step("debugging", "fix a failing check", may_block_pull_request=True),
    "e2e": _Step("end-to-end", "test the app end to end", may_block_pull_request=True),
    "retro": _Step("wrap-up", "note lessons from the run", may_block_pull_request=False),
    "architectural_review": _Step("design review", "fix design problems", may_block_pull_request=False),
}
_NEXT_STEP = _Step("next", "continue the work", may_block_pull_request=False)
_DECLINE_PREFIX = "The step stays stuck until the folder changes or the factory can check it safely"


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

    def action_for(self, task: Task) -> ActionRequest:
        action = self.action
        action["task_id"] = task.pk
        return action

    def record(self) -> None:
        self.worktree.merge_extra(set_keys={"attested_state": self.recorded})


def checkout_rows(path: str) -> list[Worktree]:
    return [
        row
        for row in Worktree.objects.exclude(extra__worktree_path__isnull=True).only("extra", "repo_path", "branch")
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


def _options(attestation: CheckoutAttestation, decline: str) -> tuple[CardOption, CardOption]:
    accept = (
        "The step resumes on the folder as it is; changes the factory did not make become part of its work."
        if attestation.host_evidence
        else "The step runs once on the folder as it is; "
        "edits the factory did not make, even unfinished ones, become part of its work."
    )
    return CardOption("Accept", accept, recommended=True), CardOption("Decline", decline)


def _pull_request_url(task: Task, worktree: Worktree) -> str:
    rows = PullRequest.objects.live().filter(ticket=task.ticket)
    extra = task.ticket.extra if isinstance(task.ticket.extra, dict) else {}
    by_branch = extra.get("pr_url_by_branch")
    preferred = by_branch.get(worktree.branch) if isinstance(by_branch, dict) else None
    preferred_ref = pr_ref_from_url(preferred) if isinstance(preferred, str) else None
    slug = worktree_repo_slug(worktree)
    if not slug and worktree.repo_path:
        candidates = rows.filter(repo__iendswith=f"/{worktree.repo_path}").values_list("repo", flat=True).distinct()
        names = {name.casefold(): name for name in candidates}
        if preferred_ref is not None and preferred_ref.slug.casefold().endswith(f"/{worktree.repo_path.casefold()}"):
            names[preferred_ref.slug.casefold()] = preferred_ref.slug
        slug = next(iter(names.values())) if len(names) == 1 else ""
    if not slug:
        return ""
    rows = rows.filter(repo__iexact=slug)
    if (
        isinstance(preferred, str)
        and preferred_ref is not None
        and preferred_ref.slug.casefold() == slug.casefold()
        and (
            rows.filter(url=preferred).exists()
            or not PullRequest.objects.filter(ticket=task.ticket, url=preferred).exists()
        )
    ):
        return preferred
    return rows.order_by("pk").values_list("url", flat=True).first() or ""


def _pull_request_words(url: str, *, may_block: bool) -> tuple[str, str]:
    if url and may_block:
        return (
            f"it may hold up <{url}|this pull request> while open",
            f"{_DECLINE_PREFIX}; the pull request may wait while open.",
        )
    if url:
        return (
            f"this ticket also has <{url}|an open pull request>",
            f"{_DECLINE_PREFIX}; that pull request was open when asked.",
        )
    return (
        "no pull request for this folder was open when asked",
        f"{_DECLINE_PREFIX}; no pull request for this folder was open when asked.",
    )


def _unlinked_title(title: str) -> str:
    return f"{title.removesuffix('...')}\u2026" if title.endswith("...") else title


def attest_card(task: Task, attestation: CheckoutAttestation) -> tuple[str, QuestionCard]:
    ticket = task.ticket
    title = safe_ticket_card_title(ticket.short_description, ticket.extra, "this ticket")
    # Outside a link, a trailing "..." followed by more words reads as a sentence end.
    subject = f"<{ticket.issue_url}|{title}>" if ticket.issue_url else _unlinked_title(title)
    step = _STEPS.get(normalize_phase(task.phase), _NEXT_STEP)
    pull_request = _pull_request_url(task, attestation.worktree)
    waits, decline = _pull_request_words(pull_request, may_block=step.may_block_pull_request)
    state = attestation.state
    process_fact = (
        "Nothing is running in the checkout."
        if attestation.host_evidence
        else "The host process list could not rule out live processes."
    )
    holdings = f"{counted(state.commits_ahead, 'commit')} and {counted(state.uncommitted, 'uncommitted file')}"
    doubt = (
        f"nothing is running in its folder; it changed and holds {holdings}"
        if attestation.host_evidence
        else f"its folder holds {holdings}; the factory cannot rule out another editor"
    )
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=(
            *(("The checkout differs from its last recorded state.",) if attestation.host_evidence else ()),
            process_fact,
            f"It holds {counted(state.commits_ahead, 'commit')} not on the target branch.",
            f"It has {counted(state.uncommitted, 'uncommitted file')}.",
        ),
        blocker="Only the owner may accept a checkout state the factory did not record.",
        why=f"It will {step.work}; {waits}; {doubt}.",
        options=_options(attestation, decline),
        action=attestation.action_for(task),
    )
    return f"Should the {step.label} step of {subject} run on its working folder as it is now?", card


def ask_owner_to_attest(task: Task, attestation: CheckoutAttestation) -> DeferredQuestion | None:
    try:
        marker = action_marker(attestation.action_for(task))
        latest = (
            DeferredQuestion.objects.filter(dedupe_marker=marker, answered_at__isnull=False)
            .order_by("-answered_at", "-pk")
            .first()
        )
        if latest is not None and latest.parked_task_id == task.pk and owner_declined(latest):
            return None
        unresolved = (
            DeferredQuestion.objects.filter(
                evidence__action__kind=OwnerActionKind.ATTEST,
                evidence__action__target=str(attestation.worktree.pk),
                evidence__action__binding=attestation.state.digest,
                evidence__action__host_evidence=attestation.host_evidence,
                answered_at__isnull=False,
            )
            .exclude(audits__action__in=("carried_out", "declined", "refused"))
            .order_by("-answered_at", "-pk")
        )
        if waiting := next((row for row in unresolved if not owner_declined(row)), None):
            return waiting
        question, card = attest_card(task, attestation)
        with transaction.atomic():
            held = DeferredQuestion.owner_pending().select_for_update().filter(dedupe_marker=marker).first()
            if held is not None and (
                held.question != question
                or held.evidence != card.to_evidence()
                or held.options_json != card.options_json()
            ):
                held.mark_stale(
                    "The checkout card changed since this question was asked", resolver_id="checkout_attest"
                )
            obsolete = (
                DeferredQuestion.owner_pending()
                .filter(
                    evidence__action__kind=OwnerActionKind.ATTEST,
                    evidence__action__target=str(attestation.worktree.pk),
                )
                .filter(parked_task=task)
                .exclude(dedupe_marker=marker)
            )
            for row in obsolete.select_for_update():
                row.mark_stale("The checkout changed since this question was asked", resolver_id="checkout_attest")
            return DeferredQuestion.record(question, dedupe_marker=marker, parked_task=task, card=card)
    except (DatabaseError, DeferredQuestionError):
        logger.exception("Could not record checkout attestation question for task %s", task.pk)
        return None
