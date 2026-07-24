"""The owner DM thread — one identity and one resolution record for both directions.

Two concerns address the same object from opposite sides. Questions flow IN: the
headless lane and (since souliane/teatree#3642) the interactive lane both record a
:class:`~teatree.core.models.deferred_question.DeferredQuestion` mirrored to a Slack DM,
and the resurfacing side re-raises an unanswered one IN ITS ORIGINAL THREAD. Noise flows
OUT: the hourly sweep (souliane/teatree#3658) resolves threads that no longer need the
owner. If those two sides carried their own notion of thread identity or their own
resolution state they would contradict each other — the sweep closing what resurfacing
is about to raise, or resurfacing re-raising what the sweep just closed.

So both go through here. The thread IDENTITY is the mirrored ``(slack_channel,
slack_ts)`` pair the question was posted under, and the RESOLUTION RECORD is the
question row's own single-use ``dismissed_at`` / ``answered_at`` stamp. There is no
second table and no second state machine to drift.

Nothing here posts. A resolution is a state change the owner sees as the thread simply
dropping out of their queue; the sweep's whole point is to REDUCE what they read.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from teatree.core.models.deferred_question import DeferredQuestion

#: Below this age a thread is ordinary hygiene the sweep may auto-resolve. Past it, an
#: unanswered question is a real backlog item the owner has not got to — closing it
#: would hide it, so the sweep leaves it for the resurfacing side (#3658).
AUTO_RESOLVE_MAX_AGE = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class OwnerThread:
    """One owner DM thread, identified by the Slack coordinates of its question."""

    question: DeferredQuestion

    @property
    def channel(self) -> str:
        return self.question.slack_channel

    @property
    def ts(self) -> str:
        return self.question.slack_ts

    @property
    def created_at(self) -> datetime:
        return self.question.created_at

    def age(self, *, now: datetime | None = None) -> timedelta:
        return (now or timezone.now()) - self.question.created_at

    def auto_resolvable(self, *, now: datetime | None = None) -> bool:
        """Whether this thread is young enough for the sweep to close on its own."""
        return self.age(now=now) <= AUTO_RESOLVE_MAX_AGE


def open_owner_threads(*, since: datetime | None = None, now: datetime | None = None) -> tuple[OwnerThread, ...]:
    """Every unresolved owner-directed thread the sweep and resurfacing both see.

    *since* is the sweep's watermark: it widens the pass to threads opened after the
    last one. Threads still inside :data:`AUTO_RESOLVE_MAX_AGE` are ALWAYS included
    regardless of the watermark, so a thread that opened before the last pass and
    became resolvable since is not skipped forever. Oldest first.
    """
    pending = DeferredQuestion.pending().filter(audience=DeferredQuestion.Audience.OWNER_QUESTION)
    cutoff = (now or timezone.now()) - AUTO_RESOLVE_MAX_AGE
    rows = [row for row in pending if since is None or row.created_at >= since or row.created_at >= cutoff]
    return tuple(OwnerThread(question=row) for row in rows)


def resolve_owner_thread(thread: OwnerThread, *, reason: str) -> bool:
    """Close *thread* with *reason*; ``True`` on the transition, ``False`` if already closed.

    The one write both directions share. Single-use by construction
    (:meth:`~teatree.core.models.deferred_question.DeferredQuestion.mark_stale` is a
    guarded CAS), so a resolution racing an owner's answer never overwrites the answer.
    """
    question = thread.question
    if not question.is_pending:
        return False
    question.mark_stale(reason)
    return question.dismissed_at is not None
