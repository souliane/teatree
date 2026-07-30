"""The owner-DM hygiene pass — resolve what no longer needs the owner (#3658).

The other half of "never lose an open question". Resurfacing only works if the queue is
honest: a queue full of already-handled threads trains the owner to ignore it, at which
point the resurfacing guarantee is worthless. So this pass reads the same
:mod:`teatree.core.owner_threads` queue the resurfacing side reads, and closes only what
it can decide DETERMINISTICALLY:

*   the owner already replied in the thread;
*   the thread's subject is closed (its pull request merged, its ticket landed);
*   the thread duplicates an older thread that is still open.

Everything else is left alone. A wrong auto-resolve is worse than a leftover thread,
because the owner stops trusting the queue — so every rule below is a positive proof of
"handled", never an absence of evidence. Anything older than
:data:`~teatree.core.owner_threads.AUTO_RESOLVE_MAX_AGE` is untouched on principle: a
question that has sat for more than a day is real backlog, not hygiene.

The pass posts NOTHING. Resolving is the whole output, and it is visible as the thread
dropping out of the owner's queue. A pass with nothing to do therefore says nothing at
all, which is the point.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from teatree.core.models.deferred_question import question_fingerprint
from teatree.core.owner_threads import OwnerThread, open_owner_threads, resolve_owner_thread
from teatree.url_classify import find_forge_urls

logger = logging.getLogger(__name__)

_OWNER_REPLIED = "the owner already replied in this thread"
_SUBJECT_CLOSED = "the thread's subject is closed"
_DUPLICATE = "duplicate of an older thread that is still open"


@dataclass(frozen=True, slots=True)
class SweepSeams:
    """Injectable probes so the pass is exercisable without Slack or a forge.

    ``owner_replied`` needs a live Slack read and is ``None`` when no messaging backend
    is wired — the rule is then skipped rather than guessed. ``subject_closed`` defaults
    to :func:`subject_closed_locally`, which costs nothing.
    """

    owner_replied: Callable[[OwnerThread], bool] | None = None
    subject_closed: Callable[[str], bool] | None = None


@dataclass(frozen=True, slots=True)
class SweepResult:
    """What one pass did — ``resolved`` is also the loop's "should I say anything" test."""

    resolved: int
    left_open: int
    reasons: tuple[str, ...] = ()

    @property
    def silent(self) -> bool:
        return self.resolved == 0


def run_sweep(
    *,
    since: datetime | None = None,
    now: datetime | None = None,
    seams: SweepSeams | None = None,
) -> SweepResult:
    """Resolve every confidently-handled owner thread since *since*; leave the rest."""
    resolved_seams = seams or SweepSeams()
    threads = open_owner_threads(since=since, now=now)
    still_open: list[OwnerThread] = []
    reasons: list[str] = []
    for thread in threads:
        reason = _resolution_reason(thread, still_open, seams=resolved_seams, now=now)
        if reason is None or not resolve_owner_thread(thread, reason=reason):
            still_open.append(thread)
            continue
        reasons.append(reason)
    if reasons:
        logger.info("dm_sweep resolved %d owner thread(s): %s", len(reasons), "; ".join(sorted(set(reasons))))
    return SweepResult(resolved=len(reasons), left_open=len(still_open), reasons=tuple(reasons))


def _resolution_reason(
    thread: OwnerThread,
    still_open: list[OwnerThread],
    *,
    seams: SweepSeams,
    now: datetime | None,
) -> str | None:
    """Why *thread* no longer needs the owner, or ``None`` to leave it open.

    *still_open* is the threads this pass has already decided to keep, in age order, so
    the duplicate rule can retire the NEWER copy and keep the one the owner has had
    longest — the opposite choice would silently reset the age of a waiting question.
    """
    if not thread.auto_resolvable(now=now):
        return None
    if seams.owner_replied is not None and seams.owner_replied(thread):
        return _OWNER_REPLIED
    closed = seams.subject_closed or subject_closed_locally
    if closed(thread.question.question):
        return _SUBJECT_CLOSED
    fingerprint = question_fingerprint(thread.question.question)
    if any(question_fingerprint(kept.question.question) == fingerprint for kept in still_open):
        return _DUPLICATE
    return None


def subject_closed_locally(question_text: str) -> bool:
    """Whether every forge reference in *question_text* is landed, per teatree's own records.

    Model-free and network-free: the factory already tracks the tickets it worked, so a
    thread about a pull request it merged is answerable without asking anyone. Requires
    at least one reference and ALL of them landed — a question that also names an open
    PR is still live. A text with no reference is not a subject-closed thread.
    """
    from teatree.core.models.ticket import Ticket  # noqa: PLC0415 — ORM import needs the app registry

    urls = find_forge_urls(question_text)
    if not urls:
        return False
    landed = {Ticket.State.MERGED, Ticket.State.RETROSPECTED, Ticket.State.DELIVERED}
    for url in urls:
        states = set(Ticket.objects.filter(pull_requests__url=url).values_list("state", flat=True)) | set(
            Ticket.objects.filter(issue_url=url).values_list("state", flat=True)
        )
        if not states or not states <= landed:
            return False
    return True
