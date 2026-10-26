"""Recheck and attest a checkout after a direct owner command or Slack approval."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from django.db import DatabaseError, transaction
from django.utils import timezone

from teatree.core.cleanup.process_table import read_process_table
from teatree.core.gates.pr_budget_gate import open_pr_urls_for_repo
from teatree.core.modelkit.owner_action import ActionRequest, OwnerActionKind, action_marker
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.phases import ARCHITECTURAL_REVIEW_PHASE, normalize_phase
from teatree.core.modelkit.question_card import CardOption, QuestionCard, counted, safe_ticket_card_title
from teatree.core.models import PullRequest, Task, Ticket, Worktree
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError
from teatree.core.models.question_actions import owner_declined
from teatree.core.models.worktree_occupancy import occupancy_holder
from teatree.core.notify import resolve_owner_dm_backend
from teatree.core.owner_question_message import NO_LONGER_NEEDED, closed_message, replace_root
from teatree.core.worktree.branch_owner import worktree_repo_slug
from teatree.core.worktree.checkout_state import CheckoutState, checkout_state
from teatree.core.worktree.worktree_paths import paths_match
from teatree.utils.run import TimeoutExpired
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
    ARCHITECTURAL_REVIEW_PHASE: _Step("design review", "fix design problems", may_block_pull_request=False),
}
_NEXT_STEP = _Step("next", "continue the work", may_block_pull_request=False)
_DECLINE_PREFIX = "The step stays stuck until the folder changes or the factory can check it safely"
_FALLBACK_TITLE = "this ticket"
_SAFE_PR_SLUG = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\Z")
_SAFE_PR_HOST = re.compile(r"[a-z0-9.-]+\Z")


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
    worktree.merge_extra(
        set_keys={
            "attested_state": attestation.recorded,
            "direct_attested_state": attestation.state.digest,
            "direct_attested_at": timezone.now().isoformat(),
        }
    )
    return attestation


def declined_attest_tasks(worktree: Worktree, digest: str, *, task_id: int | None = None) -> set[int]:
    extra = worktree.get_extra()
    direct_at = None
    if extra.get("direct_attested_state") == digest and isinstance(extra.get("direct_attested_at"), str):
        try:
            parsed = datetime.fromisoformat(extra["direct_attested_at"])
            direct_at = parsed if parsed.tzinfo is not None else None
        except ValueError:
            pass
    answered = DeferredQuestion.objects.filter(
        answered_at__isnull=False,
        evidence__action__kind=OwnerActionKind.ATTEST,
        evidence__action__target=str(worktree.pk),
        evidence__action__binding=digest,
    )
    shared = answered.exclude(evidence__action__has_key="task_id").order_by("-answered_at", "-pk").first()
    shared_declined = (
        shared is not None
        and shared.answered_at is not None
        and owner_declined(shared)
        and (direct_at is None or shared.answered_at > direct_at)
    )
    shared_tasks = (
        set(Task.objects.filter(ticket=worktree.ticket).values_list("pk", flat=True))
        if shared_declined and task_id is None
        else ({task_id} if shared_declined and task_id is not None else set())
    )
    answered = (
        answered.filter(parked_task__ticket=worktree.ticket)
        if task_id is None
        else answered.filter(parked_task_id=task_id)
    )
    answered = answered.order_by("answered_at", "pk")
    latest = {card.parked_task_id: card for card in answered}
    return shared_tasks | {
        task_id
        for task_id, card in latest.items()
        if task_id is not None
        and card.answered_at is not None
        and owner_declined(card)
        and (direct_at is None or card.answered_at > direct_at)
    }


def retire_card(row: DeferredQuestion, reason: str, *, resolver_id: str) -> None:
    row.mark_stale(reason, resolver_id=resolver_id)
    if row.dismissed_at is not None and row.slack_ts:
        transaction.on_commit(lambda: _close_posted_card(row))


def _close_posted_card(row: DeferredQuestion) -> None:
    try:
        backend = resolve_owner_dm_backend()[0]
        if backend is not None:
            replace_root(row, closed_message(row, NO_LONGER_NEEDED), backend)
    except Exception:
        logger.exception("Could not close the posted checkout question %s", row.pk)


def _options(attestation: CheckoutAttestation, decline: str) -> tuple[CardOption, CardOption]:
    accept = (
        "All eligible waiting steps for this folder resume; changes the factory did not make become part of their work."
        if attestation.host_evidence
        else "All eligible waiting steps for this folder resume; "
        "edits the factory did not make, even unfinished ones, become part of their work."
    )
    return CardOption("Accept", accept, recommended=True), CardOption("Decline", decline)


def _safe_pr_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        ref = pr_ref_from_url(url)
        host = parsed.hostname or ""
    except ValueError:
        return ""
    if ref is None or parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        return ""
    if not _SAFE_PR_HOST.fullmatch(host) or not _SAFE_PR_SLUG.fullmatch(ref.slug) or parsed.netloc.casefold() != host:
        return ""
    if "/-/merge_requests/" in parsed.path:
        path = "-/merge_requests"
    elif "/merge_requests/" in parsed.path:
        path = "merge_requests"
    else:
        path = "pull"
    return f"https://{host}/{ref.slug}/{path}/{ref.pr_id}"


def _pull_request_url(task: Task, worktree: Worktree) -> str:
    rows = PullRequest.objects.live().filter(ticket=task.ticket)
    extra = task.ticket.extra if isinstance(task.ticket.extra, dict) else {}
    by_branch = extra.get("pr_url_by_branch")
    preferred = by_branch.get(worktree.branch) if isinstance(by_branch, dict) else None
    preferred_url = _safe_pr_url(preferred) if isinstance(preferred, str) else ""
    preferred_ref = pr_ref_from_url(preferred_url) if preferred_url else None
    try:
        slug = worktree_repo_slug(worktree, timeout=5)
    except (OSError, TimeoutExpired):
        logger.exception("Could not read the worktree repository for checkout card %s", worktree.pk)
        slug = ""
    if not slug and worktree.repo_path:
        candidates = rows.filter(repo__iendswith=f"/{worktree.repo_path}").values_list("repo", flat=True).distinct()
        names = {name.casefold(): name for name in candidates}
        if preferred_ref is not None and preferred_ref.slug.casefold().endswith(f"/{worktree.repo_path.casefold()}"):
            names[preferred_ref.slug.casefold()] = preferred_ref.slug
        slug = next(iter(names.values())) if len(names) == 1 else ""
    if not slug:
        return ""
    rows = rows.filter(repo__iexact=slug)
    if preferred_url and preferred_ref is not None and preferred_ref.slug.casefold() == slug.casefold():
        known = PullRequest.objects.filter(ticket=task.ticket)
        known_same = [row for row in known if _safe_pr_url(row.url) == preferred_url]
        available = open_pr_urls_for_repo(task.ticket, slug) | set(rows.values_list("url", flat=True))
        live_known = any(row.state not in {PullRequest.State.CLOSED, PullRequest.State.MERGED} for row in known_same)
        if preferred in available and (not known_same or live_known):
            return preferred_url
        if known_same:
            return ""
    return _safe_pr_url(rows.order_by("pk").values_list("url", flat=True).first() or "")


def _pull_request_words(url: str, *, may_block: bool) -> tuple[str, str]:
    if url and may_block:
        return (
            f"it may hold up <{url}|this pull request> while open",
            f"{_DECLINE_PREFIX}; the pull request remains open.",
        )
    if url:
        return (
            f"this ticket also has <{url}|an open pull request>",
            f"{_DECLINE_PREFIX}; the pull request remains open.",
        )
    return (
        "no pull request for this folder was open when asked",
        f"{_DECLINE_PREFIX}; no pull request waits.",
    )


def _question(step: _Step, ticket: Ticket, title: str) -> str:
    # Outside a link, a trailing "..." or a sentence mark followed by more words reads as a sentence end.
    unlinked = f"{title.removesuffix('...')}\u2026" if title.endswith("...") else title.rstrip(".?!") or _FALLBACK_TITLE
    subject = f"<{ticket.issue_url}|{title}>" if ticket.issue_url else unlinked
    return f"Should the {step.label} step of {subject} run on its working folder as it is?"


def attest_card(task: Task, attestation: CheckoutAttestation) -> tuple[str, QuestionCard]:
    ticket = task.ticket
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
        action=attestation.action,
    )
    question = _question(step, ticket, safe_ticket_card_title(ticket.short_description, ticket.extra, _FALLBACK_TITLE))
    return (_question(step, ticket, _FALLBACK_TITLE) if card.problems(question) else question), card


def ask_owner_to_attest(task: Task, attestation: CheckoutAttestation) -> DeferredQuestion | None:
    try:
        if task.pk in declined_attest_tasks(attestation.worktree, attestation.state.digest, task_id=task.pk):
            return None
        marker = action_marker(attestation.action)
        unresolved = (
            DeferredQuestion.objects.awaiting_action_outcome()
            .filter(
                evidence__action__kind=OwnerActionKind.ATTEST,
                evidence__action__target=str(attestation.worktree.pk),
                evidence__action__binding=attestation.state.digest,
            )
            .order_by("-answered_at", "-pk")
        )
        if waiting := next((row for row in unresolved if not owner_declined(row)), None):
            return waiting
        question, card = attest_card(task, attestation)
        with transaction.atomic():
            held = (
                DeferredQuestion.owner_pending()
                .select_for_update()
                .filter(
                    evidence__action__kind=OwnerActionKind.ATTEST,
                    evidence__action__target=str(attestation.worktree.pk),
                    evidence__action__binding=attestation.state.digest,
                )
                .order_by("-slack_ts", "-pk")
                .first()
            )
            if held is not None:
                if held.slack_ts:
                    return held
                changed = (
                    held.evidence.get("action") != card.action
                    or held.evidence.get("checked") != list(card.checked)
                    or held.options_json != card.options_json()
                    or re.findall(r"<(https://[^|>]+)\|", str(held.evidence.get("why", "")))
                    != re.findall(r"<(https://[^|>]+)\|", card.why)
                    or (held.question == question and held.evidence != card.to_evidence())
                )
                if changed:
                    retire_card(
                        held, "The checkout card changed since this question was asked", resolver_id="checkout_attest"
                    )
                else:
                    return held
            obsolete = (
                DeferredQuestion.owner_pending()
                .filter(
                    evidence__action__kind=OwnerActionKind.ATTEST,
                    evidence__action__target=str(attestation.worktree.pk),
                )
                .exclude(dedupe_marker=marker)
            )
            for row in obsolete.select_for_update():
                retire_card(row, "The checkout changed since this question was asked", resolver_id="checkout_attest")
            return DeferredQuestion.record(question, dedupe_marker=marker, parked_task=task, card=card)
    except (DatabaseError, DeferredQuestionError):
        logger.exception("Could not record checkout attestation question for task %s", task.pk)
        return None
