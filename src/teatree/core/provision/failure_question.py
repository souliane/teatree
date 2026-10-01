"""The question a failed intake provision raises, and the proof that its trigger has healed.

``Ticket.begin_planning`` starts a ticket before ``workspace ticket`` attaches its repos,
so "no repos on ticket" is usually transient: ``execute_provision`` retries it on
:data:`NO_REPOS_RETRY_DELAYS` before asking. A question that was asked is withdrawn once
:func:`provision_failures_healed` proves every repo on its ticket has a live checkout.
"""

import datetime as dt
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

from teatree.core.models import Ticket, Worktree
from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.worktree.worktree_roots import CheckoutState, probe_checkout

PROVISION_FAILURE_MARKER_RE = re.compile(r"provision-failure:([0-9]+)")

#: Covers the observed ~1h45m between intake and the phase agent's ``workspace ticket``,
#: well inside the 24h stale-READY expiry of the job queue.
NO_REPOS_RETRY_DELAYS: tuple[dt.timedelta, ...] = tuple(
    dt.timedelta(minutes=minutes) for minutes in (2, 5, 15, 30, 60, 120)
)


def provision_failure_marker(ticket_pk: int) -> str:
    return f"provision-failure:{ticket_pk}"


def no_repos_retry_delay(attempt: int) -> dt.timedelta | None:
    """The wait before retry ``attempt + 1``, or ``None`` once the budget is spent."""
    if 0 <= attempt < len(NO_REPOS_RETRY_DELAYS):
        return NO_REPOS_RETRY_DELAYS[attempt]
    return None


def record_provision_failure_question(ticket: Ticket, detail: str, *, retries: int = 0) -> DeferredQuestion:
    """Surface an un-provisionable ticket as one durable, deduped ``DeferredQuestion``.

    A ticket with no repos has nothing to drop and does not hold planning back, so it is
    offered only the retry or ignoring it.
    """
    where = ticket.issue_url or f"ticket {ticket.pk}"
    overlay = ticket.overlay or "<overlay>"
    # `workspace ticket` is the idempotent seam that reaches checkout CREATION; `worktree provision` only resolves one.
    retry = (
        f"`t3 {overlay} workspace ticket {ticket.issue_url}`"
        if ticket.issue_url
        else f"`t3 {overlay} workspace ticket` re-run against this ticket's issue reference"
    )
    if ticket.repos:
        question = (
            f"Provision failed on {where} (overlay {overlay}): {detail}. Every repo must provision before "
            f"planning starts, so the ticket holds at STARTED until this one does. Retry checkout creation "
            f"from the venue that owns the worktrees ({retry}), or drop the repo from the ticket?"
        )
    else:
        tried = f" after {retries} automatic retries" if retries else ""
        question = (
            f"Provision failed on {where} (overlay {overlay}): {detail}{tried}, so there is nothing to check out. "
            f"Attach the repos by re-running checkout creation from the venue that owns the worktrees ({retry}), "
            f"or ignore the ticket?"
        )
    return DeferredQuestion.record(question, dedupe_marker=provision_failure_marker(int(ticket.pk)))


def provision_failures_healed(questions: Sequence[DeferredQuestion]) -> dict[int, str]:
    """Question pk -> reason, for each provision-failure row whose ticket now has every repo live.

    Positive-only: a missing ticket, a ticket with no repos, or any repo short of a
    checkout ``probe_checkout`` proves live keeps the row. A recorded path alone is a
    claim — the provisioner keeps a reused row's stale path when re-provisioning fails.
    """
    tickets = {
        question.pk: int(match.group(1))
        for question in questions
        if (match := PROVISION_FAILURE_MARKER_RE.fullmatch(question.dedupe_marker))
    }
    if not tickets:
        return {}
    repos = dict(Ticket.objects.filter(pk__in=set(tickets.values())).values_list("pk", "repos"))
    live: dict[int, set[str]] = defaultdict(set)
    for worktree in Worktree.objects.filter(ticket_id__in=repos):
        path = worktree.worktree_path
        if path and probe_checkout(Path(path)) is CheckoutState.CHECKOUT:
            live[worktree.ticket_id].add(worktree.repo_path)
    return {
        question_pk: f"ticket {ticket_pk} now has a live checkout for every repo ({', '.join(wanted)})"
        for question_pk, ticket_pk in tickets.items()
        if (wanted := list(repos.get(ticket_pk) or [])) and set(wanted) <= live[ticket_pk]
    }
