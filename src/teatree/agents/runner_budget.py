"""Per-ticket cumulative cost cap for the agent runner.

Split out of :mod:`teatree.agents.runner` for the module-health LOC cap.
"""

from dataclasses import dataclass

from django.db.models import Sum

from teatree.config import get_effective_settings
from teatree.core.models import TaskAttempt, Ticket


@dataclass(frozen=True)
class TicketBudget:
    """Per-ticket cumulative cost cap consumer (#885 / #398-4).

    Where ``LoopWatchdog`` bounds a *single in-flight run* (it interrupts a
    runaway mid-run from the heartbeat thread), this consumer bounds the
    *whole ticket's lifetime spend* at dispatch time. Before a task's agent is
    launched it sums ``TaskAttempt.cost_usd`` across every task under the
    ticket; once the cumulative spend crosses the configured ceiling no
    further attempt is dispatched and a ``budget_exceeded`` ``TaskAttempt``
    failure is recorded (``task.fail()`` runs), surfacing the breach on the
    failure record. A ceiling of ``0.0`` disables the cap.
    """

    max_cost_usd: float

    @classmethod
    def from_settings(cls) -> "TicketBudget":
        """Build the budget from effective DB-home config."""
        effective = get_effective_settings()
        return cls(max_cost_usd=float(effective.ticket_budget_max_cost_usd))

    def breach_reason(self, ticket: Ticket) -> str | None:
        """Return a reason string with the observed total, or ``None`` if healthy."""
        if not self.max_cost_usd:
            return None
        total = TaskAttempt.objects.filter(task__ticket=ticket).aggregate(cost=Sum("cost_usd"))["cost"] or 0.0
        if total > self.max_cost_usd:
            return (
                f"budget_exceeded: ticket spent ${total:.2f} > cap ${self.max_cost_usd:.2f} — refusing further dispatch"
            )
        return None
