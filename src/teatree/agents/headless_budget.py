"""Per-ticket cumulative cost cap for the headless runner.

Split out of :mod:`teatree.agents.headless` for the module-health LOC cap (the
same reason ``_headless_options.py`` and ``headless_usage.py`` were split
out). Re-exported from ``teatree.agents.headless`` so ``from
teatree.agents.headless import TicketBudget`` stays valid.
"""

from dataclasses import dataclass

from django.conf import settings
from django.db.models import Sum

from teatree.config import UserSettings, get_effective_settings
from teatree.core.models import TaskAttempt, Ticket

# Conservative documented default (#885 / #398-4): the per-ticket cumulative
# cost cap is opt-in. ``0.0`` = disabled, so installing this consumer changes
# no behaviour until the user configures a ceiling — the same precedent #882
# set for the watchdog's absolute cost dimension. The user picks a ceiling
# that matches their budget appetite once they want batch runs bounded.
_DEFAULT_TICKET_BUDGET = {
    "max_cost_usd": 0.0,  # 0 = disabled
}


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
        """Build the budget from the DB-home config tier; Django-settings as fallback.

        The cap resolves through ``get_effective_settings()`` (the #1775 config tier), so
        ``config_setting get`` sees it (F9.5). The legacy Django-settings
        ``TEATREE_TICKET_BUDGET`` dict stays a documented fallback: it supplies the value
        only while the config field is still at its dataclass default (unconfigured), so an
        explicit DB / env config always wins.
        """
        effective = get_effective_settings()
        fallback = getattr(settings, "TEATREE_TICKET_BUDGET", None) or _DEFAULT_TICKET_BUDGET
        default = UserSettings().ticket_budget_max_cost_usd
        configured = effective.ticket_budget_max_cost_usd
        value = configured if configured != default else fallback.get("max_cost_usd", 0.0)
        return cls(max_cost_usd=float(value))

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
