"""Re-open a discharged colleague review when the author pushes a new head.

The gap this closes (2026-07-22 incident): the factory reviewed a colleague
MR once, the reviewer ticket short-circuited to ``REVIEW_POSTED``, and the MR was
never looked at again. Four hours after the review landed on one such MR the
author pushed 146 commits and GitLab reset the approvals — nothing happened,
because no live ticket was left and nothing watches a discharged review.

Why a scanner over a terminal ticket, and not a new non-terminal state
---------------------------------------------------------------------

``REVIEW_POSTED`` is the honest state for a reviewer ticket: the obligation IS
discharged — at the SHA that was reviewed. There is genuinely nothing to do
until the author pushes. A new ``watching`` state would keep every reviewed
MR permanently in-flight on the statusline and would force edits to
``_TERMINAL_STATES``, ``_WORK_STATE_ORDER``, ``POST_REVIEW_STATES``, the
orphan sweep and every state-completeness partition test — a large blast
radius for a reviewer-only concern.

Instead this scanner leans on machinery the FSM already has:

* ``Ticket.extra["reviewed_sha"]`` / ``["last_review_state"]`` — the durable
    "reviewed at SHA X" record, written by ``mark_reviewed_externally`` /
    ``mark_review_no_action`` and the ``ReviewerPrsScanner`` cache.
* ``reviewer_pr.new_sha`` — the existing signal kind, already routed to
    ``t3:reviewer`` in ``dispatch_tables``.
* ``persistence._handle_reviewer`` — already re-stamps ``reviewed_sha``,
    drops the stale ``last_review_state``, short-circuits on an open reviewing
    task, and dedups via ``_already_reviewed_at_head``.
* ``mark_reviewed_externally``'s ``REVIEW_POSTED`` self-transition — so the
    second review can actually complete.

So this module adds one thing only: the observation that the head moved.

Why ``ReviewerPrsScanner`` cannot do this
-----------------------------------------

That scanner lists ``host.list_review_requested_prs`` — a forge
reviewer-*assignment* filter. A colleague MR discovered from a Slack review
broadcast never gets a forge assignment, so it is permanently absent from
that scan (its own ``_orphaned_task_signals`` docstring says exactly this).
Every MR in the incident arrived that way. This scanner is keyed on the local
reviewer tickets instead, so it covers every discovery route uniformly.

Loop-safety
-----------

Two failure modes are designed out rather than patched:

* **Self-retrigger.** The trigger is the head commit and nothing else. The
    factory's own outputs — inline notes, an approval, a Slack reaction — never
    move the head, so the factory cannot re-trigger itself. Approval-reset is
    deliberately NOT a trigger here: it is a *consequence* of the push the head
    SHA already reports, and keying on it would fire on forge-side approval
    churn the factory itself causes.
* **Same-SHA churn.** A ticket is watched only when it carries a recorded
    ``reviewed_sha``; the signal fires only on a different, non-empty live
    head; and an open reviewing task suppresses it entirely. An unreadable live
    head (``""``) is "cannot confirm", never "moved".
"""

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from teatree.core.backend_protocols import CodeHostBackend, PrOpenState, ReviewState
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.url_specificity import best_url_match_specificity
from teatree.utils.url_slug import pr_ref_from_url

if TYPE_CHECKING:
    from teatree.core.models import Task, Ticket

logger = logging.getLogger(__name__)

# Only a TERMINAL review observation means "this review is discharged". An
# in-progress or absent state is the ``ReviewerPrsScanner`` / broadcast
# path's business, not a re-review.
_DISCHARGED_REVIEW_STATES: frozenset[str] = frozenset(
    {ReviewState.APPROVED.value, ReviewState.REVIEWED_NO_ACTION.value},
)


def _discharged_sha(ticket: "Ticket") -> str:
    """The SHA this ticket's review was discharged at, or ``""`` when it was not.

    Both halves of the pair are required: a ``reviewed_sha`` with no terminal
    ``last_review_state`` is an in-flight observation the other review paths
    own, and a terminal state with no SHA is the pre-fix shape this scanner
    must never act on.
    """
    extra = ticket.extra or {}
    if extra.get("last_review_state") not in _DISCHARGED_REVIEW_STATES:
        return ""
    sha = extra.get("reviewed_sha")
    return sha if isinstance(sha, str) else ""


@dataclass(slots=True)
class ReviewedPrHeadScanner:
    """Emit ``reviewer_pr.new_sha`` for reviewer tickets whose reviewed head moved.

    ``overlay_name`` scopes the watch set so a multi-overlay tick does not
    make one overlay's scanner re-review a sibling's MRs; ``max_checks`` caps
    the per-tick forge calls so a large backlog degrades into "checked next
    tick" instead of a slow tick. ``allowed_url_prefixes`` /
    ``competing_url_prefixes`` apply the same per-overlay URL claim the
    reviewer/my-PR scanners use (#1015, #1324).
    """

    host: CodeHostBackend
    overlay_name: str = ""
    allowed_url_prefixes: tuple[str, ...] = field(default_factory=tuple)
    competing_url_prefixes: tuple[str, ...] = field(default_factory=tuple)
    max_checks: int = 20
    name: str = "reviewed_pr_head"

    def scan(self) -> list[ScanSignal]:
        signals: list[ScanSignal] = []
        for ticket in self._watched_tickets():
            try:
                signal = self._signal_for_ticket(ticket)
            except Exception:
                logger.exception("ReviewedPrHeadScanner failed on %s", ticket.issue_url)
                continue
            if signal is not None:
                signals.append(signal)
        return signals

    def _watched_tickets(self) -> list["Ticket"]:
        """Reviewer tickets with a discharged review and no reviewing task in flight."""
        from django.apps import apps  # noqa: PLC0415 — deferred: app registry read at call time

        ticket_model = cast("type[Ticket]", apps.get_model("core", "Ticket"))
        task_model = cast("type[Task]", apps.get_model("core", "Task"))
        candidates = ticket_model.objects.filter(role="reviewer").exclude(issue_url="")
        if self.overlay_name:
            candidates = candidates.filter(overlay=self.overlay_name)
        candidates = candidates.exclude(tasks__status__in=task_model.Status.active(), tasks__phase="reviewing")
        watched = [
            ticket
            for ticket in candidates.order_by("pk").distinct()
            if _discharged_sha(ticket) and self._url_allowed(ticket.issue_url)
        ]
        return watched[: self.max_checks]

    def _signal_for_ticket(self, ticket: "Ticket") -> ScanSignal | None:
        url = ticket.issue_url
        ref = pr_ref_from_url(url)
        if ref is None:
            return None
        previous = _discharged_sha(ticket)
        head = self.host.fetch_live_head_sha(slug=ref.slug, pr_id=ref.pr_id).strip()
        if not head or head.lower() == previous.lower():
            # Empty head = the forge call could not confirm anything. Never
            # re-review on doubt; the next tick asks again.
            return None
        if self._pr_is_closed(url):
            return None
        return ScanSignal(
            kind="reviewer_pr.new_sha",
            summary=f"Review needed (head moved since review): {url}",
            payload={
                "url": url,
                "head_sha": head,
                "previous_sha": previous,
                "overlay": self.overlay_name,
                "trigger": "reviewed_head_moved",
            },
        )

    def _pr_is_closed(self, url: str) -> bool:
        """True only on PROVEN merged/closed — OPEN and UNKNOWN both keep the review live."""
        try:
            state = self.host.get_pr_open_state(pr_url=url)
        except Exception:
            logger.exception("ReviewedPrHeadScanner could not read PR state for %s", url)
            return False
        return state in {PrOpenState.MERGED, PrOpenState.CLOSED}

    def _url_allowed(self, url: str) -> bool:
        """Same per-overlay URL-prefix claim as ``ReviewerPrsScanner._url_allowed`` (#1015, #1324)."""
        if not self.allowed_url_prefixes:
            return True
        own = best_url_match_specificity(url, self.allowed_url_prefixes)
        if own == 0:
            return False
        return best_url_match_specificity(url, self.competing_url_prefixes) <= own
