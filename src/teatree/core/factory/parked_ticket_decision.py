"""The owner's retire-or-keep decision on tickets parked after a failed newest task."""

import hashlib
import logging
from collections import Counter

from django.db import transaction
from django.db.models import QuerySet

from teatree.core.factory.health_signal import HealthSignal, SignalCollection
from teatree.core.factory.parked_tickets import TaskPair, aged_parked_tasks, still_parked, task_pair
from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, safe_card_title
from teatree.core.models import Task, Ticket
from teatree.core.models.deferred_question import DeferredQuestion, DeferredQuestionError
from teatree.core.models.known_issue import KnownIssue
from teatree.core.worktree.unshipped_work import escalate_unshipped_work_once, unshipped_work_paths

PARKED_MARKER_PREFIX = "parked-tickets:"
RETIRE_ANSWER = "Retire them"
KEEP_ANSWER = "Keep them parked"
_QUESTION = "Should I retire tickets whose failed newest tasks were created over two weeks ago?"
_QUOTED_TITLES = 3
logger = logging.getLogger(__name__)


def _tickets_have(count: int) -> str:
    return "1 ticket has" if count == 1 else f"{count} tickets have"


def awaiting_decision(tasks: list[Task]) -> list[Task]:
    kept = ruled_pairs(KEEP_ANSWER)
    return [task for task in tasks if task_pair(task) not in kept]


def parked_ticket_signals() -> SignalCollection:
    count = len(awaiting_decision(aged_parked_tasks()))
    if not count:
        return SignalCollection()
    return SignalCollection(
        (
            HealthSignal(
                fingerprint="parked-tickets-await-owner",
                severity=KnownIssue.Severity.WARNING,
                kind="parked_tickets",
                summary=(
                    f"{_tickets_have(count)} failed newest tasks created over 14 days ago"
                    " — whether to retire or keep them parked is the owner's decision"
                ),
            ),
        )
    )


def _listed_pairs(tasks: list[Task]) -> list[list[int]]:
    return [list(task_pair(task)) for task in tasks]


def parked_marker(tasks: list[Task]) -> str:
    return f"{PARKED_MARKER_PREFIX}{hashlib.sha256(repr(_listed_pairs(tasks)).encode()).hexdigest()[:40]}"


def _quoted_titles(tasks: list[Task]) -> str:
    titles = [title for task in tasks if (title := safe_card_title(task.ticket.short_description, ""))]
    shown = "; ".join(titles[:_QUOTED_TITLES])
    return f"{shown}; and {len(titles) - _QUOTED_TITLES} more" if len(titles) > _QUOTED_TITLES else shown


def _shown_card_pending() -> bool:
    return (
        DeferredQuestion.owner_pending()
        .filter(dedupe_marker__startswith=PARKED_MARKER_PREFIX)
        .exclude(slack_ts="")
        .exists()
    )


def ask_owner_about_parked(tasks: list[Task]) -> DeferredQuestion | None:
    if not tasks or _shown_card_pending():
        return None
    causes = Counter((task.failure_kind or "unclassified").replace("_", " ") for task in tasks)
    cause_text = ", ".join(f"{count} {cause}" for cause, count in causes.most_common(3))
    card = QuestionCard(
        decision=OwnerDecision.IRREVERSIBLE,
        checked=(
            f"{_tickets_have(len(tasks))} a failed newest task created over two weeks ago.",
            f"Recorded causes include {cause_text}.",
        ),
        blocker="Retiring a ticket ends its current work record.",
        why="The stopped tickets need your decision before they can leave the parked queue.",
        options=(
            CardOption(
                RETIRE_ANSWER,
                "I close these work records and queue the removal of their checkouts,"
                " so any wanted issue can be picked up again.",
                recommended=True,
            ),
            CardOption(KEEP_ANSWER, "I leave these work records parked."),
        ),
    ).with_quote(_QUESTION, _quoted_titles(tasks))
    try:
        return DeferredQuestion.record(
            _QUESTION,
            dedupe_marker=parked_marker(tasks),
            card=card,
            private_context={"task_pairs": _listed_pairs(tasks)},
        )
    except DeferredQuestionError:
        logger.exception("parked-ticket owner card refused")
        return None


def _owner_answers(answer: str) -> QuerySet[DeferredQuestion]:
    return DeferredQuestion.answered_in_slack([answer]).filter(dedupe_marker__startswith=PARKED_MARKER_PREFIX)


def _card_pairs(question: DeferredQuestion) -> set[TaskPair]:
    listed = question.evidence.get("private_context", {}).get("task_pairs", [])
    return {(ticket_id, task_id) for ticket_id, task_id in listed}


def ruled_pairs(answer: str) -> set[TaskPair]:
    """The tasks the owner ruled on by giving *answer* in Slack."""
    return {pair for question in _owner_answers(answer) for pair in _card_pairs(question)}


def release_undecided_answers() -> None:
    """Ask again after a non-decision: a stray answer or an age-ceiling retirement."""
    DeferredQuestion.objects.filter(dedupe_marker__startswith=PARKED_MARKER_PREFIX).release_undecided(
        (RETIRE_ANSWER, KEEP_ANSWER)
    )


def settle_changed_parked_cards(awaiting: list[Task]) -> None:
    """Dismiss every unshown pending card that no longer describes *awaiting*, freeing its marker for a later return."""
    current = parked_marker(awaiting) if awaiting else ""
    stale = list(
        DeferredQuestion.unmirrored_pending()
        .filter(dedupe_marker__startswith=PARKED_MARKER_PREFIX)
        .exclude(dedupe_marker=current)
    )
    for question in stale:
        question.mark_stale("the parked tickets changed", resolver_id="parked_tickets_changed")
    DeferredQuestion.objects.filter(pk__in=[question.pk for question in stale]).release_markers()


def apply_retire_answers(aged: list[Task]) -> int:
    """Retire the tickets each "Retire them" answer listed, once, and count them.

    A spent answer frees its marker, which also keeps it from being applied twice: a ticket it could not retire
    is asked afresh when it parks again.
    """
    retired = 0
    spent = list(_owner_answers(RETIRE_ANSWER))
    for question in spent:
        ticket_ids = _retire_parked(_card_pairs(question), aged)
        if ticket_ids:
            logger.info("parked-tickets: owner answer %s retired tickets %s", question.pk, ticket_ids)
        retired += len(ticket_ids)
    DeferredQuestion.objects.filter(pk__in=[question.pk for question in spent]).release_markers()
    return retired


def _retire_parked(pairs: set[TaskPair], aged: list[Task]) -> list[int]:
    ticket_ids: list[int] = []
    for task in aged:
        if task_pair(task) not in pairs:
            continue
        ticket = Ticket.objects.get(pk=task.ticket_id)
        if not still_parked(ticket, task.pk):
            continue
        unshipped = unshipped_work_paths(ticket)
        with transaction.atomic():
            ticket = Ticket.objects.select_for_update().get(pk=task.ticket_id)
            if not still_parked(ticket, task.pk):
                continue
            ticket.ignore()
            ticket.save(update_fields=["state", "extra"])
            escalate_unshipped_work_once(ticket, paths=unshipped)
            ticket_ids.append(ticket.pk)
    return ticket_ids
