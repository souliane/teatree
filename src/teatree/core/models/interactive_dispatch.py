"""The durable seat an admitted INTERACTIVE dispatch holds (#4129).

:func:`~teatree.core.dispatch_admission.live_agent_count` read durable DB state via
``Task.objects.active_claims()`` — the right shape, and the lesson #4106 learned the
hard way. But it counted teatree ``Task`` rows, and the scenario the governor's third
lane exists for — an orchestrator session dispatching through the harness
``Agent``/``Task`` tool — creates no ``Task`` row at all. So an ad-hoc interactive
dispatch was never counted in the ceiling it had just been admitted against, and N
rapid dispatches on a quiet box each read the same live count and all passed.

This is that lane's ``Task.admitted_at``: one row per admitted dispatch, taken at the
gate, counted by every later probe. The seat's life ends at ``SubagentStop``, which
hands it back keyed on the terminating agent's id; :data:`SEAT_WINDOW` is the BACKSTOP
for a release that never arrives — a dispatch that never materialised, an agent the box
killed, a harness restart — not the expected lifetime. Erring long would wedge the lane
behind phantom seats; erring short reopens the burst this bounds.
"""

import datetime as dt
from typing import ClassVar

from django.db import models
from django.utils import timezone

#: How long a seat survives with no release. Covers a sub-agent's ordinary life, so the
#: ceiling bounds a POPULATION rather than a dispatch rate; a stuck lane still clears
#: within it, and the ``[admission-ok: …]`` escape and the ``admission_governor_enabled``
#: kill-switch cover the interim.
SEAT_WINDOW = dt.timedelta(minutes=30)


class InteractiveDispatchManager(models.Manager["InteractiveDispatch"]):
    """Take, count and hand back the seats — whole-table operations, never a filter."""

    def live_seats(self, *, now: dt.datetime | None = None) -> "models.QuerySet[InteractiveDispatch]":
        """Every seat still held at *now* — unreleased and inside :data:`SEAT_WINDOW`."""
        moment = now or timezone.now()
        return self.filter(released_at__isnull=True, admitted_at__gt=moment - SEAT_WINDOW)

    def claim_seat(self, *, session_id: str, ceiling: int, other_agents: int = 0) -> bool:
        """True when this dispatch took a seat under *ceiling* — else it was refused.

        The bound is arbitrated INSIDE the write, not between a probe and it (#4125). A
        count read before the insert is stale the moment it returns, which is exactly the
        burst this closes: the seat is written FIRST, then its own rank among the live
        seats is re-read, and a rank that puts the population at/over the ceiling deletes
        the row it just wrote. A racer therefore loses on its rank rather than on a count
        that never saw it.

        *other_agents* is the rest of the box's live population — the durable ``Task``
        claims — which the rank cannot see because those rows are not seats.
        """
        seat = self._open_seat(session_id)
        ahead = self.live_seats().filter(pk__lt=seat.pk).count()
        if other_agents + ahead >= ceiling:
            seat.delete()
            return False
        return True

    def record_seat(self, *, session_id: str) -> "InteractiveDispatch":
        """Take a seat with no ceiling to clear — the ceiling-EXEMPT arm's stamp.

        A sub-agent's onward dispatch keeps its documented exemption from the ceiling,
        but still puts an agent on the box: uncounted, it is invisible to the arm that
        does clamp, and the two gaps compound.
        """
        return self._open_seat(session_id)

    def release_seat(self, *, session_id: str, agent_id: str) -> bool:
        """Hand *session_id*'s oldest live seat back on *agent_id*'s termination.

        Keyed on the agent id purely for idempotency — a re-fired ``SubagentStop``
        matches its own release and gives nothing back — because the seat is taken before
        the harness has assigned an id, so no seat can be bound to its agent at birth.
        Oldest-first is the only ordering that keeps the lane's seats fungible.

        A release for a dispatch that never took a seat (the escape token, the
        kill-switch) hands back someone else's, which UNDER-counts — the fail-open
        direction this module takes everywhere.
        """
        if not agent_id or self.filter(agent_id=agent_id).exists():
            return False
        seat = self.live_seats().filter(session_id=session_id).order_by("admitted_at", "pk").first()
        if seat is None:
            return False
        seat.agent_id = agent_id
        seat.released_at = timezone.now()
        seat.save(update_fields=["agent_id", "released_at"])
        return True

    def _open_seat(self, session_id: str) -> "InteractiveDispatch":
        """Write one seat, dropping whatever the window has already expired."""
        self.filter(admitted_at__lte=timezone.now() - SEAT_WINDOW).delete()
        return self.create(session_id=session_id)


class InteractiveDispatch(models.Model):
    """One interactive ``Agent``/``Task`` dispatch the governor admitted."""

    session_id = models.CharField(max_length=200, blank=True, default="")
    #: The terminating sub-agent that handed the seat back; blank while the seat is held.
    agent_id = models.CharField(max_length=200, blank=True, default="")
    admitted_at = models.DateTimeField(default=timezone.now, db_index=True)
    released_at = models.DateTimeField(null=True, blank=True)

    objects: ClassVar[InteractiveDispatchManager] = InteractiveDispatchManager()

    class Meta:
        db_table = "teatree_interactive_dispatch"
        ordering: ClassVar = ["admitted_at"]
        indexes: ClassVar = [models.Index(fields=["released_at", "admitted_at"], name="idx_dispatch_seat_live")]

    def __str__(self) -> str:
        return f"interactive-dispatch<{self.session_id or '(no session)'}@{self.admitted_at:%Y-%m-%dT%H:%M:%S}>"
