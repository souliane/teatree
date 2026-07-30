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
    (default ``"ac-reviewing-codebase"``).
* ``architectural_review_cadence_hours: int`` — minimum age of the last
    COMPLETED review before re-firing (default 168 = 7 days).
* ``architectural_review_retry_backoff_hours: int`` — after a FAILED review
    (with no completed one since), the shorter age the failed attempt must
    reach before re-firing (default 12).
* ``architectural_review_after_merge_count: int`` — fire after this many
    ticket merges since the last review (default 25).
* ``architectural_review_disabled: bool`` — escape hatch; when True the
    wiring layer skips scanner instantiation for the affected overlay.

The scanner shares :class:`teatree.loop.scanners.phase_cadence.PhaseCadence`
with the other periodic task-queuing scanners for its dedupe / last-run /
bootstrap-cadence machinery, and adds two genuine variants of its own:

* **Bounded post-failure backoff.** Two clocks gate a re-fire: the last
    COMPLETED review drives the full ``cadence_hours`` (168h) success gate, and
    the last terminal attempt of ANY status drives a shorter
    ``retry_backoff_hours`` (12h) backoff gate. A review fires only when both
    have elapsed. So a transient failure retries in 12h (no week-long blind
    spot), a persistently failing review backs off to every 12h instead of
    storming hourly, and a completed review still suppresses for the full week
    (the 168h gate dominates the 12h one).
* **Merge-count backstop.** Beyond the time cadence, a high-velocity overlay
    fires a review after ``after_merge_count`` merges since the last completed
    one — itself gated behind the same backoff so a failing backstop can't storm.

The scanner is a pure observer that creates one :class:`Task` row of
``phase="architectural_review"`` when a trigger holds and no review task is
currently queued or in-flight. The dispatcher picks up the task through the
normal pending-task pipeline; the scanner only writes the row.

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
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from django.apps import apps
from django.db.models import Max
from django.utils import timezone

from teatree.core.modelkit.phases import ARCHITECTURAL_REVIEW_PHASE
from teatree.loop.scanners.base import ScanSignal, hours_since
from teatree.loop.scanners.phase_cadence import PhaseCadence

if TYPE_CHECKING:
    from teatree.core.models import TicketTransition as _TicketTransition

logger = logging.getLogger(__name__)

#: States that count as "merged" for the after-merge trigger. ``delivered``
#: covers the post-merge "ticket fully closed" state; ``merged`` covers the
#: PR-just-landed state. ``shipped`` is the pre-merge "PR is up" state, not
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
    kwargs. The on/off decision lives at the wiring layer
    (``architectural_review_disabled`` in core config); the scanner
    itself always scans when invoked.
    """

    overlay_name: str
    skill: str = "ac-reviewing-codebase"
    cadence_hours: int = 168
    retry_backoff_hours: int = 12
    after_merge_count: int = 25
    name: str = "architectural_review"

    def scan(self) -> list[ScanSignal]:
        if not self.overlay_name:
            return []
        cadence = PhaseCadence(self.overlay_name, phase=ARCHITECTURAL_REVIEW_PHASE, cadence_hours=self.cadence_hours)
        if cadence.in_flight_exists():
            return []

        now = timezone.now()
        last_completed_at = cadence.last_completed_run_at()
        last_attempt_at = cadence.last_terminal_run_at()
        trigger = self._evaluate_triggers(
            cadence, now=now, last_completed_at=last_completed_at, last_attempt_at=last_attempt_at
        )
        if trigger is None:
            return []

        task = cadence.queue_task(
            placeholder_issue_url=f"architectural-review://{self.overlay_name}",
            agent_id=f"architectural-review-{self.overlay_name}",
            execution_reason=f"Periodic architectural review ({trigger}) via skill: {self.skill}",
            subject=f"Architectural review: {self.overlay_name}",
            log_label="ArchitecturalReviewScanner",
        )
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

    def _evaluate_triggers(
        self,
        cadence: PhaseCadence,
        *,
        now: dt.datetime,
        last_completed_at: dt.datetime | None,
        last_attempt_at: dt.datetime | None,
    ) -> str | None:
        """Return the trigger name (``bootstrap`` / ``cadence`` / ``after_merge_count``) or None.

        Two clocks gate a re-fire. The post-failure backoff comes first: a
        terminal attempt of any status within ``retry_backoff_hours`` suppresses
        every trigger, so a repeatedly-failing review backs off to the backoff
        window instead of storming hourly. Past the backoff, the success cadence
        (168h since the last COMPLETED review) and the merge-count backstop
        decide. After a COMPLETED review the 168h cadence dominates the shorter
        12h backoff; after a FAILED one, the 12h backoff is the binding gate.
        Cadence wins over merge-count when both fire.
        """
        if last_attempt_at is not None and hours_since(last_attempt_at, now=now) < self.retry_backoff_hours:
            return None
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
