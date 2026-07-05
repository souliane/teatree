"""The durable waiting-on-you gatherer (PR-21).

A single narrow read that answers "what is waiting on the user right now?" as
a list of typed :class:`WaitingEntry`. Three of the four kinds are computed
LIVE from existing durable sources — there is no sync job and no duplicated
state, so resolving the underlying thing clears the entry by construction:

* ``question`` — an unresolved ``DeferredQuestion`` (answering or dismissing
    it drops the entry).
* ``merge_authorization`` — a ``PullRequest`` that reached ``APPROVED``
    (mergeable per its FSM state) with no covering, unconsumed ``MergeClear``
    (issuing the CLEAR, or merging the PR, drops the entry).
* ``review_request`` — a pending ``ReviewAssignment`` (approving the MR drops
    the entry).

The fourth kind, ``manual``, is the operator's own free-text
:class:`~teatree.core.models.waiting_item.WaitingItem` — the only thing the
live sources cannot see, resolved explicitly.

``overlay`` scopes the overlay-bearing kinds (merge-authorization, review-request)
to that overlay plus unscoped rows; an empty overlay scopes to everything.
Questions and manual items carry no overlay and are always included.
"""

import enum
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.utils import timezone

from teatree.core.models.deferred_question import DeferredQuestion
from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.pull_request import PullRequest
from teatree.core.models.review_assignment import ReviewAssignment
from teatree.core.models.waiting_item import WaitingItem

_QUESTION_REF_LEN = 60
_MINUTES_PER_HOUR = 60
_HOURS_PER_DAY = 24


class WaitingKind(enum.StrEnum):
    """The four kinds of thing that can be waiting on the user."""

    QUESTION = "question"
    MERGE_AUTHORIZATION = "merge_authorization"
    REVIEW_REQUEST = "review_request"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class WaitingEntry:
    """One thing waiting on the user, typed by :class:`WaitingKind`.

    ``ref`` is the human-readable subject (question snippet, PR/MR url, manual
    text); ``url`` is the clickable reference when one applies; ``age`` is how
    long it has been waiting; ``entry_id`` is the manual :class:`WaitingItem`
    pk (only kind the CLI ``resolve`` acts on), ``None`` for the live kinds.
    """

    kind: str
    ref: str
    age: timedelta = field(default=timedelta())
    url: str = ""
    entry_id: int | None = None


def format_age(age: timedelta) -> str:
    """Render *age* as a compact ``2d`` / ``5h`` / ``7m`` / ``now``."""
    seconds = max(int(age.total_seconds()), 0)
    minutes = seconds // 60
    if minutes < 1:
        return "now"
    hours = minutes // _MINUTES_PER_HOUR
    if hours < 1:
        return f"{minutes}m"
    days = hours // _HOURS_PER_DAY
    if days < 1:
        return f"{hours}h"
    return f"{days}d"


def gather_waiting(overlay: str) -> list[WaitingEntry]:
    """Return every entry currently waiting on the user, scoped by *overlay*."""
    now = timezone.now()
    return [
        *_question_entries(now),
        *_merge_authorization_entries(now, overlay),
        *_review_request_entries(now, overlay),
        *_manual_entries(now),
    ]


def _question_entries(now: datetime) -> list[WaitingEntry]:
    return [
        WaitingEntry(
            kind=WaitingKind.QUESTION,
            ref=question.question.strip().replace("\n", " ")[:_QUESTION_REF_LEN],
            age=now - question.created_at,
            entry_id=question.pk,
        )
        for question in DeferredQuestion.pending()
    ]


def _merge_authorization_entries(now: datetime, overlay: str) -> list[WaitingEntry]:
    prs = PullRequest.objects.filter(state=PullRequest.State.APPROVED).select_related("ticket")
    if overlay:
        prs = prs.filter(overlay__in=[overlay, ""])
    entries: list[WaitingEntry] = []
    for pr in prs:
        if _has_covering_clear(pr):
            continue
        waited_from = pr.review_requested_at or pr.create_verified_at or now
        entries.append(
            WaitingEntry(
                kind=WaitingKind.MERGE_AUTHORIZATION,
                ref=f"{pr.repo}#{pr.iid}",
                age=now - waited_from,
                url=pr.url,
            )
        )
    return entries


def _has_covering_clear(pr: PullRequest) -> bool:
    """True iff an unconsumed, actionable CLEAR already authorises *pr*'s merge."""
    try:
        pr_id = int(pr.iid)
    except (TypeError, ValueError):
        return False
    clears = MergeClear.objects.filter(ticket=pr.ticket, pr_id=pr_id, consumed_at__isnull=True)
    return any(clear.is_actionable() for clear in clears)


def _review_request_entries(now: datetime, overlay: str) -> list[WaitingEntry]:
    qs = ReviewAssignment.objects.filter(state=ReviewAssignment.State.PENDING)
    if overlay:
        qs = qs.filter(overlay__in=[overlay, ""])
    return [
        WaitingEntry(
            kind=WaitingKind.REVIEW_REQUEST,
            ref=assignment.mr_url,
            age=now - assignment.observed_at,
            url=assignment.mr_url,
        )
        for assignment in qs
    ]


def _manual_entries(now: datetime) -> list[WaitingEntry]:
    return [
        WaitingEntry(
            kind=WaitingKind.MANUAL,
            ref=item.text,
            age=now - item.created_at,
            entry_id=item.pk,
        )
        for item in WaitingItem.objects.open()
    ]
