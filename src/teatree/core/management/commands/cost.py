"""``t3 cost`` — SDK-equivalent spend of the loop's detached headless Agent-SDK usage.

From 2026-06-15 the Agent SDK bills headless usage against a monthly credit
(Max 20x = $200) at standard API rates. This command totals the cost captured
on each :class:`~teatree.core.models.task_attempt.TaskAttempt` for the current billing
cycle and shows it against the credit, broken down per model, with a linear
end-of-cycle projection.

Also reports GitHub's agentic-workflow ET (effective tokens) metric
(souliane/teatree#657) and splits both dollars and ET by Layer-2 lane
(subscription vs metered, souliane/teatree#2887) so the two-lane cost
strategy locked in #2565 is observable.

The lane split only covers HEADLESS attempts, matching this command's
existing scope. Under the default ambient-credential dispatch (no explicit
``agent_harness_provider`` pin) a run's lane is unattributed (``""``),
bucketed under the ``unattributed`` lane in ``per_lane_*``
(:data:`~teatree.core.cost.UNATTRIBUTED_LANE`); ``subscription`` only
appears here for a run explicitly pinned to ``subscription_oauth``.

Read-only: every query underneath is a select. The billing-cycle anchor day and
the credit are configurable via ``t3 <overlay> config_setting set
billing_cycle_anchor_day <value>`` / ``sdk_monthly_credit_usd``; with no anchor
the cycle is the calendar month.
The report is routed through the machine-output seam — JSON on stdout under
``--json``, the human view on stderr — and returned as the typed payload.
"""

from typing import IO, Annotated, TypedDict, cast

import typer
from django.utils import timezone

from teatree.core.machine_output import MachineOutputCommand, emit


class CostPayload(TypedDict):
    """The wire shape of ``t3 cost --json``."""

    chip: str
    cycle_start: str
    cycle_to_date_usd: float
    credit_usd: float
    projected_month_end_usd: float
    attempts: int
    per_model_usd: dict[str, float]
    effective_tokens_total: float
    per_lane_usd: dict[str, float]
    per_lane_effective_tokens: dict[str, float]
    estimated_usd: float
    per_lane_cache_hit_ratio: dict[str, float]
    per_phase_cache_hit_ratio: dict[str, float]


class Command(MachineOutputCommand):
    def handle(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the human view."),
        ] = False,
    ) -> CostPayload:
        """Print cycle-to-date SDK-equivalent spend vs the monthly credit."""
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps command import light
        from teatree.core.cost import (  # noqa: PLC0415 — deferred: keeps command import light
            CostReport,
            cycle_start,
            cycle_start_datetime,
        )
        from teatree.core.models.task_attempt import TaskAttempt  # noqa: PLC0415 — deferred: ORM/app-registry

        settings = get_effective_settings()
        anchor = settings.billing_cycle_anchor_day or None
        today = timezone.localdate()
        start_dt = cycle_start_datetime(today, anchor_day=anchor)

        breakdown = TaskAttempt.objects.filter(started_at__gte=start_dt).cost_breakdown()
        report = CostReport.build(
            breakdown,
            credit_usd=settings.sdk_monthly_credit_usd,
            cycle_start_date=cycle_start(today, anchor_day=anchor),
            today=today,
            anchor_day=anchor,
        )

        payload: CostPayload = {
            "chip": report.chip(),
            "cycle_start": report.cycle_start_date.isoformat(),
            "cycle_to_date_usd": round(breakdown.total_usd, 4),
            "credit_usd": report.credit_usd,
            "projected_month_end_usd": round(report.projected_month_end_usd, 4),
            "attempts": breakdown.attempts,
            "per_model_usd": {tier: round(amount, 4) for tier, amount in breakdown.per_tier_usd.items()},
            "effective_tokens_total": round(breakdown.effective_tokens_total, 2),
            "per_lane_usd": {lane: round(amount, 4) for lane, amount in breakdown.per_lane_usd.items()},
            "per_lane_effective_tokens": {
                lane: round(amount, 2) for lane, amount in breakdown.per_lane_effective_tokens.items()
            },
            "estimated_usd": round(breakdown.estimated_usd, 4),
            "per_lane_cache_hit_ratio": {
                lane: round(ratio, 4) for lane, ratio in breakdown.per_lane_cache_hit_ratio.items()
            },
            "per_phase_cache_hit_ratio": {
                phase: round(ratio, 4) for phase, ratio in breakdown.per_phase_cache_hit_ratio.items()
            },
        }
        self.print_result = False
        emit(
            payload,
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human="\n".join(report.render_lines()),
        )
        return payload
