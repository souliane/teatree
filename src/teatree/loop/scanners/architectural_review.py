"""Periodic architectural-review scanner — #1136 / #1152.

The loop has long wanted a recurring "step back and review the
codebase" cadence that fires on either a time-based interval (default 7
days) or a merge-count interval (default 25 merges since last review).
The architectural review is a teatree-CORE platform behaviour — it
applies uniformly to every overlay's worktrees, not as a per-overlay
opt-in. The cadence + skill name live in teatree-core config (DB-home
:class:`teatree.config.UserSettings` in the ``ConfigSetting`` store),
with optional per-overlay overrides in ``[overlays.<name>]`` for
environments that need to tune one overlay differently from the rest:

* ``architectural_review_skill: str`` — which review skill to dispatch
    (default ``"architectural-review"``).
* ``architectural_review_cadence_hours: int`` — minimum age of the last
    COMPLETED review before re-firing (default 168 = 7 days).
* ``architectural_review_after_merge_count: int`` — fire after this many
    ticket merges since the last review (default 25).

The scanner shares :class:`teatree.loop.scanners.phase_cadence.PhaseCadence`
with the other periodic task-queuing scanners for its dedupe / last-run /
bootstrap-cadence machinery, and adds one genuine variant of its own:

* **Merge-count backstop.** Beyond the time cadence, a high-velocity overlay
    fires a review after ``after_merge_count`` merges since the last completed
    one.

One clock gates a re-fire: the last COMPLETED review against ``cadence_hours``.
A failed review reaches the transient requeue sweep for one same-day retry on
the plans lane; a terminal failure is reported to the owner. The scanner
continues to check daily for a due review.

The scanner creates one :class:`Task` row of
``phase="architectural_review"`` when a trigger holds and no review task is
currently queued or in-flight. The normal post-save admission path can enqueue
it immediately; a held task emits a reason and an owner receipt.

Design notes
------------

* No new model field for the cadence clock. The "last review" timestamp
    is the existing ``Session.started_at`` (``auto_now_add``) of the most
    recent COMPLETED ``architectural_review`` task.
* Placeholder ticket. The architectural review is per-overlay, not
    per-issue, so :class:`PhaseCadence` ``get_or_create``s a synthetic Ticket
    carrying a stable ``issue_url`` (``architectural-review://<overlay>``) to
    anchor the FK chain. The ticket carries no FSM state.
"""

import datetime as dt
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db.models import Max
from django.utils import timezone

from teatree.core.modelkit.phases import ARCHITECTURAL_REVIEW_PHASE
from teatree.core.owner_dm.architectural_review_notice import announce_held
from teatree.core.task_dispatch import pending_admission_reason
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.phase_cadence import PhaseCadence

if TYPE_CHECKING:
    from teatree.core.models import Task
    from teatree.core.models import TicketTransition as _TicketTransition

#: States that count as "merged" for the after-merge trigger. ``delivered``
#: covers the post-merge "ticket fully closed" state; ``merged`` covers the
#: PR-just-landed state. ``pr_opened`` is the pre-merge "PR is up" state, not
#: a merge.
_MERGED_STATES: frozenset[str] = frozenset({"merged", "delivered"})


@dataclass(slots=True)
class ArchitecturalReviewScanner:
    """Queue a periodic ``architectural_review`` task per overlay.

    The scanner runs per overlay; the loop's job builder fans it out
    from each :class:`OverlayBackends`. Configuration fields are passed
    explicitly (rather than read from a global at scan time) so test
    setup is deterministic and the wiring layer is the single place
    that resolves :class:`teatree.config.UserSettings` to scanner
    kwargs. The on/off decision is the ``arch_review`` Loop row; the scanner
    itself always scans when invoked.
    """

    overlay_name: str
    skill: str = "architectural-review"
    cadence_hours: int = 168
    after_merge_count: int = 25
    name: str = "architectural_review"

    def scan(self) -> list[ScanSignal]:
        cadence = PhaseCadence(
            self.overlay_name,
            phase=ARCHITECTURAL_REVIEW_PHASE,
            cadence_hours=self.cadence_hours,
            health_fingerprint=f"architectural-review-held:{self.overlay_name}",
            health_kind="architectural_review_held",
        )
        if not self.overlay_name:
            return cadence.hold("no overlay anchor", kind="architectural_review.held", retry=True)
        if cadence.in_flight_exists():
            task_model = cast("type[Task]", apps.get_model("core", "Task"))
            pending = (
                task_model.objects.in_flight_for_phase(self.overlay_name, ARCHITECTURAL_REVIEW_PHASE)
                .waiting_for_admission()
                .first()
            )
            return (
                self._held_signal(pending, cadence)
                if pending is not None
                else cadence.not_due("task already in flight", kind="architectural_review.not_due")
            )

        now = timezone.now()
        last_completed_at = cadence.last_completed_run_at()
        trigger = self._evaluate_triggers(cadence, now=now, last_completed_at=last_completed_at)
        if trigger is None:
            next_due_at = cast("dt.datetime", last_completed_at) + dt.timedelta(hours=self.cadence_hours)
            return cadence.not_due(
                f"until {next_due_at.isoformat()}",
                kind="architectural_review.not_due",
                details={"next_due_at": next_due_at.isoformat()},
            )

        task = cadence.queue_task(
            placeholder_issue_url=f"architectural-review://{self.overlay_name}",
            agent_id=f"architectural-review-{self.overlay_name}",
            execution_reason=f"Periodic architectural review ({trigger}) via skill: {self.skill}",
            subject=f"Architectural review: {self.overlay_name}",
            log_label="ArchitecturalReviewScanner",
        )
        if task is None:
            return cadence.hold("task was not created", kind="architectural_review.held", retry=True)
        task.refresh_from_db(fields=["admitted_at"])
        if task.admitted_at is None:
            held = self._held_signal(task, cadence)
            if held:
                return held
        cadence.resolve_hold()
        cadence.resolve_not_due()
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

    @staticmethod
    def _held_signal(task: "Task", cadence: PhaseCadence) -> list[ScanSignal]:
        reason = pending_admission_reason(ARCHITECTURAL_REVIEW_PHASE) or "task waiting for admission"
        announce_held(task, reason, hold_kind="admission")
        return cadence.hold(reason, kind="architectural_review.held", retry=True, task_id=task.pk)

    def _evaluate_triggers(
        self,
        cadence: PhaseCadence,
        *,
        now: dt.datetime,
        last_completed_at: dt.datetime | None,
    ) -> str | None:
        """Return the trigger name (``bootstrap`` / ``cadence`` / ``after_merge_count``) or None.

        The last COMPLETED review is the only clock: a failed one leaves it
        untouched, so the next re-fire is decided by the loop's own cadence.
        Cadence wins over merge-count when both fire.
        """
        if last_completed_at is None:
            return "bootstrap"
        # Non-None ``last_completed_at`` can only yield "cadence" or None here.
        if cadence.evaluate_trigger(now=now, last_run_at=last_completed_at) == "cadence":
            return "cadence"
        if self._count_merges_since(last_completed_at) >= self.after_merge_count:
            return "after_merge_count"
        return None

    def _count_merges_since(self, last_review_at: dt.datetime) -> int:
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


def _transition_model() -> "type[_TicketTransition] | None":
    try:
        return cast("type[_TicketTransition]", apps.get_model("core", "TicketTransition"))
    except Exception:  # noqa: BLE001 — a probe failure must never break the tick; degrade to no signal
        return None


__all__ = [
    "ARCHITECTURAL_REVIEW_PHASE",
    "ArchitecturalReviewScanner",
]
