"""What the metered lane has spent in the operator's window, and against what ceiling.

The subscription lane's budget is read from the provider's own rate-limit rows; the
metered lane has no such feed, so nothing in the pressure model measured it at all and
four runs walked into a provider cycle limit unbounded (#4816). This is the ledger a
metered ceiling is keyed on — the ORM read lives here rather than in
:mod:`teatree.core.admission_governor` because that module is at its public-function cap.

**Governed in TOKENS, never in dollars.** Token counts are what the provider reports;
``cost_usd`` on this lane is ``cost_is_estimated`` on every row — teatree's own
price-table arithmetic — and a ceiling keyed on an estimate whose error is unknown is
not a ceiling. The estimate is QUOTED as context and labelled as one.
"""

import datetime as dt
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: The literal a quoted USD figure carries, so no reader can mistake teatree's
#: price-table arithmetic for something the provider billed.
ESTIMATE_LABEL = "ESTIMATED (price-table arithmetic, not a billed amount)"


@dataclass(frozen=True, slots=True)
class MeteredSpend:
    """The metered lane's measured spend over the operator's window, and its ceiling.

    ``fresh`` is False when the ledger could not be read; the caller then contributes no
    component, the rule every unknown here follows — a probe that cannot answer must not
    be able to brake a lane.
    """

    fresh: bool
    tokens: int
    ceiling: int
    unknown_attempts: int
    estimated_cost_usd: float
    window_hours: int

    def utilization(self) -> float:
        """Spend as a fraction of the ceiling — ``1.0`` AT it, the watermark every dimension uses.

        A ceiling of ``0`` is the shipped default and means UNSET: teatree cannot read the
        operator's provider-side cycle limit and must not invent one, so it reads ``0.0``
        and the whole change stays behaviourally inert until a ceiling is configured.
        """
        if not self.fresh or self.ceiling <= 0:
            return 0.0
        return self.tokens / self.ceiling

    def detail(self) -> str:
        """The one sentence a refusal on this dimension reads.

        It names the FLOOR caveat explicitly: an attempt whose spend was unreadable
        contributes nothing to the total, so a reader told only the number would take a
        lower bound for a measurement.
        """
        floor = (
            f" — the figure is a FLOOR: {self.unknown_attempts} attempt(s) in this window recorded UNKNOWN usage"
            if self.unknown_attempts
            else ""
        )
        return (
            f"metered lane spent {self.tokens:,} tokens of the {self.ceiling:,} ceiling "
            f"over the last {self.window_hours}h "
            f"(~${self.estimated_cost_usd:.2f} {ESTIMATE_LABEL}){floor}"
        )


def read_metered_spend(*, now: dt.datetime | None = None) -> MeteredSpend:
    """Sum the metered lane's recorded spend over the operator's window.

    A read that raises returns ``fresh=False`` rather than a zero: the two are opposite
    claims, and only the first is honest about a ledger nobody could open.
    """
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django app-registry read at call time

    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: avoids a config import cycle

    try:
        settings = get_effective_settings()
        ceiling = int(settings.metered_token_ceiling)
        window_hours = int(settings.metered_spend_window_hours)
        moment = now or timezone.now()
        tokens, unknown, cost = _window_totals(moment - dt.timedelta(hours=window_hours))
    except Exception:
        logger.exception("metered spend ledger unreadable — contributing no pressure component")
        return MeteredSpend(
            fresh=False, tokens=0, ceiling=0, unknown_attempts=0, estimated_cost_usd=0.0, window_hours=0
        )
    return MeteredSpend(
        fresh=True,
        tokens=tokens,
        ceiling=ceiling,
        unknown_attempts=unknown,
        estimated_cost_usd=cost,
        window_hours=window_hours,
    )


def _window_totals(window_start: dt.datetime) -> tuple[int, int, float]:
    """``(tokens, unknown_attempts, estimated_cost_usd)`` for metered attempts since *window_start*."""
    from django.db.models import Count, Q, Sum  # noqa: PLC0415 — deferred: Django app-registry read at call time

    from teatree.core.models.task_attempt import TaskAttempt  # noqa: PLC0415 — deferred: same

    rows = TaskAttempt.objects.filter(lane=TaskAttempt.Lane.METERED, ended_at__gte=window_start).aggregate(
        input_tokens=Sum("input_tokens"),
        output_tokens=Sum("output_tokens"),
        cost=Sum("cost_usd"),
        unknown=Count("pk", filter=Q(usage_unknown=True)),
    )
    tokens = (rows["input_tokens"] or 0) + (rows["output_tokens"] or 0)
    return tokens, rows["unknown"] or 0, float(rows["cost"] or 0.0)


__all__ = ["ESTIMATE_LABEL", "MeteredSpend", "read_metered_spend"]
