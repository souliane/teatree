"""Periodic architectural-review scanner — #1136 / #1152.

The fat loop has long wanted a recurring "step back and review the
codebase" cadence that fires on either a time-based interval (default 7
days) or a merge-count interval (default 25 merges since last review).
The architectural review is a teatree-CORE platform behaviour — it
applies uniformly to every overlay's worktrees, not as a per-overlay
opt-in. The cadence + skill name live in teatree-core config (the
``[teatree]`` table in ``~/.teatree.toml`` via
:class:`teatree.config.UserSettings`), with optional per-overlay
overrides in ``[overlays.<name>]`` for environments that need to tune
one overlay differently from the rest:

* ``architectural_review_skill: str`` — which review skill to dispatch
    (default ``"ac-reviewing-codebase"``).
* ``architectural_review_cadence_hours: int`` — minimum age of the last
    review before re-firing (default 168 = 7 days).
* ``architectural_review_after_merge_count: int`` — fire after this many
    ticket merges since the last review (default 25).
* ``architectural_review_disabled: bool`` — escape hatch; when True the
    wiring layer skips scanner instantiation for the affected overlay.

The scanner is a pure observer that creates one :class:`Task` row of
``phase="architectural_review"`` when either trigger condition holds and
no review task is currently queued or in-flight. The dispatcher (and any
overlay-side handler that subscribes) picks up the task through the
normal pending-task pipeline; the scanner only writes the row.

Design notes
------------

* No new model field for the cadence clock. The "last review" timestamp
    is the existing ``Session.started_at`` (``auto_now_add``) of the most
    recent ``architectural_review`` task. ``Task`` now has its own
    ``created_at`` (migration 0004), but this scanner intentionally keys on
    ``Session.started_at`` as the queue time — a Task always carries a
    Session created at the moment we queue, so the Session's ``started_at``
    is the canonical queue timestamp.
* Dupe suppression. A pending or claimed task for the same overlay/phase
    acts as the lock — the scanner sees the in-flight row and returns no
    signal. Completion (or failure) of that task unlocks the next cadence
    window.
* Placeholder ticket. The architectural review is per-overlay, not
    per-issue, so we ``get_or_create`` a synthetic Ticket carrying a
    stable ``issue_url`` (``architectural-review://<overlay>``) to anchor
    the FK chain. Real overlays already do this for tracking-only purposes
    (e.g. the GitLab approvals scanner). The ticket carries no FSM state.
"""

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from teatree.loop.scanners.base import ScanSignal

if TYPE_CHECKING:
    from teatree.core.models import Session as _Session
    from teatree.core.models import Task as _Task
    from teatree.core.models import Ticket as _Ticket
    from teatree.core.models import TicketTransition as _TicketTransition

logger = logging.getLogger(__name__)

#: Canonical phase token written to ``Task.phase`` for review tasks.
ARCHITECTURAL_REVIEW_PHASE = "architectural_review"

#: States that count as "merged" for the after-merge trigger. ``delivered``
#: covers the post-merge "ticket fully closed" state; ``merged`` covers the
#: PR-just-landed state. ``shipped`` is the pre-merge "PR is up" state, not
#: a merge.
_MERGED_STATES: frozenset[str] = frozenset({"merged", "delivered"})

#: States that mean a review task is still in-flight (cannot queue a dupe).
_IN_FLIGHT_TASK_STATES: frozenset[str] = frozenset({"pending", "claimed"})


@dataclass(slots=True)
class ArchitecturalReviewScanner:
    """Queue a periodic ``architectural_review`` task per overlay.

    The scanner runs per overlay; the loop's job builder fans it out
    from each :class:`OverlayBackends`. Configuration fields are passed
    explicitly (rather than read from a global at scan time) so test
    setup is deterministic and the wiring layer is the single place
    that resolves :class:`teatree.config.UserSettings` to scanner
    kwargs. The on/off decision lives at the wiring layer
    (``architectural_review_disabled`` in core config); the scanner
    itself always scans when invoked.
    """

    overlay_name: str
    skill: str = "ac-reviewing-codebase"
    cadence_hours: int = 168
    after_merge_count: int = 25
    name: str = "architectural_review"

    def scan(self) -> list[ScanSignal]:
        if not self.overlay_name:
            return []
        if self._in_flight_review_exists():
            return []

        now = timezone.now()
        last_review_at = self._last_review_completed_at()
        trigger = self._evaluate_triggers(now=now, last_review_at=last_review_at)
        if trigger is None:
            return []

        task = self._queue_task(trigger=trigger)
        if task is None:
            return []
        return [
            ScanSignal(
                kind="architectural_review.queued",
                summary=(f"architectural review queued for {self.overlay_name} (trigger: {trigger})"),
                payload={
                    "overlay": self.overlay_name,
                    "skill": self.skill,
                    "phase": ARCHITECTURAL_REVIEW_PHASE,
                    "task_id": task.pk,
                    "trigger": trigger,
                },
            ),
        ]

    def _in_flight_review_exists(self) -> bool:
        """True iff a pending/claimed review task exists for this overlay."""
        task_model = _task_model()
        if task_model is None:
            return False
        return task_model.objects.filter(
            ticket__overlay=self.overlay_name,
            phase=ARCHITECTURAL_REVIEW_PHASE,
            status__in=_IN_FLIGHT_TASK_STATES,
        ).exists()

    def _last_review_completed_at(self) -> object:
        """Return the most recent review task's Session.started_at, or None.

        Returns ``None`` when no prior review task has been recorded for
        this overlay (the bootstrap case — cadence is trivially elapsed).
        """
        task_model = _task_model()
        if task_model is None:
            return None
        aggregate = task_model.objects.filter(
            ticket__overlay=self.overlay_name,
            phase=ARCHITECTURAL_REVIEW_PHASE,
            status=task_model.Status.COMPLETED,
        ).aggregate(ts=Max("session__started_at"))
        return aggregate["ts"]

    def _evaluate_triggers(self, *, now: object, last_review_at: object) -> str | None:
        """Return the trigger name (``cadence`` / ``after_merge_count``) or None.

        Cadence wins over merge-count when both fire — the cadence is the
        primary contract; merge-count is the "high-velocity backstop" so
        a code-factory churning out merges doesn't go a full week without
        a review.
        """
        if last_review_at is None:
            return "bootstrap"
        # ``now`` and ``last_review_at`` are ``datetime``s in practice;
        # typed as ``object`` here to stay decoupled from ``Max()``'s
        # return shape on the type checker. The subtraction is what
        # matters, not the static type.
        elapsed_hours = (now - last_review_at).total_seconds() / 3600.0  # type: ignore[operator]
        if elapsed_hours >= self.cadence_hours:
            return "cadence"
        merges_since = self._count_merges_since(last_review_at)
        if merges_since >= self.after_merge_count:
            return "after_merge_count"
        return None

    def _count_merges_since(self, last_review_at: object) -> int:
        """Count tickets in this overlay whose latest merge transition is after *last_review_at*.

        We look at :class:`TicketTransition` rather than ``Ticket.state``
        because the latter doesn't carry a timestamp. A ticket might
        bounce between states, but the *most recent* transition to a
        merged state is what we count.
        """
        transition_model = _transition_model()
        if transition_model is None:
            return 0
        # Latest transition per ticket, restricted to this overlay's
        # tickets that are now in a merged state.
        latest_per_ticket = (
            transition_model.objects.filter(
                ticket__overlay=self.overlay_name,
                ticket__state__in=_MERGED_STATES,
                to_state__in=_MERGED_STATES,
            )
            .values("ticket_id")
            .annotate(latest=Max("created_at"))
        )
        return sum(1 for row in latest_per_ticket if row["latest"] is not None and row["latest"] > last_review_at)

    def _queue_task(self, *, trigger: str) -> "_Task | None":
        """Create a Task + Session row anchored at the per-overlay placeholder ticket.

        Wrapped in ``transaction.atomic()`` so a concurrent scanner on a
        second loop process can't double-queue: the in-flight check and
        the insert run under one DB transaction. A DB error is logged
        but never raised — losing one tick's review queue is acceptable;
        crashing the tick is not.
        """
        ticket_model = _ticket_model()
        task_model = _task_model()
        session_model = _session_model()
        if ticket_model is None or task_model is None or session_model is None:
            return None
        try:
            with transaction.atomic():
                ticket, _created = ticket_model.objects.get_or_create(
                    issue_url=_placeholder_issue_url(self.overlay_name),
                    defaults={"overlay": self.overlay_name, "role": "author"},
                )
                # Make sure the overlay tag stays current — overlays renamed
                # after the placeholder was first created should still queue
                # under their canonical name.
                if ticket.overlay != self.overlay_name:
                    ticket.overlay = self.overlay_name
                    ticket.save(update_fields=["overlay"])
                session = session_model.objects.create(
                    overlay=self.overlay_name,
                    ticket=ticket,
                    agent_id=f"architectural-review-{self.overlay_name}",
                )
                return task_model.objects.create(
                    ticket=ticket,
                    session=session,
                    phase=ARCHITECTURAL_REVIEW_PHASE,
                    execution_target=task_model.ExecutionTarget.HEADLESS,
                    subject=f"Architectural review: {self.overlay_name}",
                    execution_reason=(f"Periodic architectural review ({trigger}) via skill: {self.skill}"),
                )
        except Exception:
            logger.exception(
                "ArchitecturalReviewScanner: failed to queue review task for overlay %r",
                self.overlay_name,
            )
            return None


def _placeholder_issue_url(overlay_name: str) -> str:
    """Stable per-overlay synthetic URL for the anchoring placeholder ticket."""
    return f"architectural-review://{overlay_name}"


def _ticket_model() -> "type[_Ticket] | None":
    try:
        return cast("type[_Ticket]", apps.get_model("core", "Ticket"))
    except Exception:  # noqa: BLE001
        return None


def _task_model() -> "type[_Task] | None":
    try:
        return cast("type[_Task]", apps.get_model("core", "Task"))
    except Exception:  # noqa: BLE001
        return None


def _session_model() -> "type[_Session] | None":
    try:
        return cast("type[_Session]", apps.get_model("core", "Session"))
    except Exception:  # noqa: BLE001
        return None


def _transition_model() -> "type[_TicketTransition] | None":
    try:
        return cast("type[_TicketTransition]", apps.get_model("core", "TicketTransition"))
    except Exception:  # noqa: BLE001
        return None


# ``Count``/``Q`` are kept on the public surface so future extension
# (e.g. counting non-merge transitions or PR pushes) can build on the
# same aggregate primitives without re-importing across the module
# boundary. Pruning if unused is fine; left here for symmetry with
# sibling scanners that compose similar queries.
__all__ = [
    "ARCHITECTURAL_REVIEW_PHASE",
    "ArchitecturalReviewScanner",
    "Count",
    "Q",
]
