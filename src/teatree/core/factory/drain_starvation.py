"""Draining work queued while none of it runs — the stall's own signature (#4374).

The measured shape: three ``coding`` agents held every slot for over half an hour, five
``reviewing`` rows queued behind them, and zero reviews running. Every surface read
healthy — the worker was busy, the loop ticked, no error was raised anywhere — and the
board simply stopped moving, because reviewing and shipping are what RETIRE a pull request
and none of them could get in.

That state is invisible unless something names it, which is what this module is: the
reservation in :mod:`teatree.core.agent_admission` is the fix, and this is the alarm for
the state the fix exists to prevent — a rollback to ``drain_slot_reservation = 0``, a
reservation too small for the box, or a lane the seat window has gone soft on all present
this way. Like :mod:`teatree.core.intake.budget` it reads state and decides nothing, and
it is the ONLY reader, so the doctor and any later surface cannot hold two opinions about
whether the drain lane is starved.
"""

from dataclasses import dataclass
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

#: How long queued draining work must go unrun before it reads as starvation rather than
#: the ordinary gap between one review finishing and the next being picked up. Long enough
#: that a healthy handover never trips it; short enough that the measured 34-minute stall
#: is reported while it is still happening.
DRAIN_STARVED_AFTER = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class DrainLaneState:
    """The draining class's lane as one reading, plus the verdict drawn from it."""

    pending: int
    running: int
    longest_wait: timedelta | None

    @property
    def starved(self) -> bool:
        """True when draining work is queued, none of it is running, and it has waited.

        Every clause is decisive: a live agent means the lane is moving however deep the
        queue, ``longest_wait is None`` is the empty queue an idle box has, and without the
        threshold the alarm would fire on the ordinary gap between one review finishing and
        the next being picked up.
        """
        if self.running or self.longest_wait is None:
            return False
        return self.longest_wait >= DRAIN_STARVED_AFTER

    def report(self) -> str:
        """The one-line reason the board stopped moving, in the terms an operator can act on."""
        waited = f"{int((self.longest_wait or timedelta()).total_seconds() // 60)}m"
        return (
            f"drain lane starved: {self.pending} reviewing/shipping task(s) queued, none running, "
            f"oldest waiting {waited} — expensive work holds every slot and nothing can retire a PR"
        )


def read_drain_lane_state() -> DrainLaneState:
    """Read the draining class's queue depth, its live agents, and its longest wait."""
    from teatree.core.modelkit.phases import cheap_phase_spellings  # noqa: PLC0415 — deferred with the ORM read
    from teatree.core.models import Task  # noqa: PLC0415 — ORM import needs the app registry

    now = timezone.now()
    draining = Q(phase__in=cheap_phase_spellings())
    # Dispatchable only: a queued row no (role, phase) pair routes will never run whatever
    # the lane does, so counting it would report a starvation no capacity can relieve.
    queued = Task.objects.filter(draining, Task.dispatchable_q(), status=Task.Status.PENDING)
    oldest = queued.order_by("created_at").values_list("created_at", flat=True).first()
    return DrainLaneState(
        pending=queued.count(),
        running=Task.objects.filter(draining, status=Task.Status.CLAIMED, lease_expires_at__gt=now).count(),
        longest_wait=now - oldest if oldest else None,
    )


__all__ = ["DRAIN_STARVED_AFTER", "DrainLaneState", "read_drain_lane_state"]
