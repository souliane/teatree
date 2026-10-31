"""Surface a retired ticket's uncommitted work to the owner instead of vetoing the retirement."""

import logging

from teatree.core.modelkit.owner_decision import OwnerDecision
from teatree.core.modelkit.question_card import CardOption, QuestionCard, safe_card_title
from teatree.core.models import Ticket, ticket_worktree_checks
from teatree.core.models.deferred_question import DeferredQuestion

logger = logging.getLogger(__name__)

#: One question per ticket, ever: the factory's own guard drops a duplicate only while the earlier one is
#: unanswered, and answering it does not move the row off IGNORED, so a retirement would re-ask forever.
UNSHIPPED_WORK_MARKER = "issue-closed-unshipped-work:{pk}"


def unshipped_work_paths(ticket: Ticket) -> list[str]:
    """The ticket's worktrees holding uncommitted tracked changes; empty when unreadable."""
    try:
        return ticket_worktree_checks.collect_dirty_worktree_paths(ticket)
    except Exception:
        logger.exception("Could not read worktree state for ticket %s — retiring without the probe", ticket.pk)
        return []


def escalate_unshipped_work_once(ticket: Ticket, *, paths: list[str]) -> None:
    if not paths:
        return
    question = "Should I save the unfinished work from a retired ticket to a pull request?"
    DeferredQuestion.record(
        question,
        session_id="",
        dedupe_marker=UNSHIPPED_WORK_MARKER.format(pk=ticket.pk),
        card=QuestionCard(
            decision=OwnerDecision.IRREVERSIBLE,
            checked=("The ticket was retired.", "The working copy holds uncommitted tracked changes."),
            blocker="Only the owner can say whether the work may be thrown away.",
            why=f"The ticket was retired, but {len(paths)} working copy(ies) still hold changes nobody saved.",
            options=(
                CardOption("Save it", "I open a pull request so the work is not lost.", recommended=True),
                CardOption("Discard it", "I delete the changes for good."),
            ),
            subject=f"ticket:{ticket.pk}",
        ).with_quote(question, safe_card_title(ticket.short_description, "")),
    )
