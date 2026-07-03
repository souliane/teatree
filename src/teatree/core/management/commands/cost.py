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
existing scope (the credit tracks headless spend only — see below). Under
the default ambient-credential dispatch (no explicit
``agent_harness_provider`` pin) a headless run's lane is unattributed
(``""``), bucketed under the ``unattributed`` lane in ``per_lane_*``
(:data:`~teatree.core.cost.UNATTRIBUTED_LANE`); ``subscription`` only
appears here for a headless run explicitly pinned to ``subscription_oauth``.
Interactive ``/loop`` sub-agent turns ride the user's own subscription too
and DO carry ``lane=subscription`` on their ``TaskAttempt`` row, but that
row's ``execution_target`` is ``interactive`` so it is excluded from this
report by design — see ``TaskAttempt.objects.usages()`` for the full,
lane-unfiltered picture across both execution targets.

Read-only: every query underneath is a select. The billing-cycle anchor day and
the credit are configurable in ``~/.teatree.toml`` (``billing_cycle_anchor_day``
/ ``sdk_monthly_credit_usd``); with no anchor the cycle is the calendar month.
The structured value is the return (django-typer serialises it) — JSON when
``--json``, else the human report.
"""

import json
from typing import Annotated

import typer
from django.utils import timezone
from django_typer.management import TyperCommand


class Command(TyperCommand):
    def handle(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the structured report as JSON instead of the human view."),
        ] = False,
    ) -> str:
        """Print cycle-to-date SDK-equivalent spend vs the monthly credit."""
        from teatree.config import get_effective_settings  # noqa: PLC0415
        from teatree.core.cost import CostReport, cycle_start, cycle_start_datetime  # noqa: PLC0415
        from teatree.core.models.task_attempt import TaskAttempt  # noqa: PLC0415

        settings = get_effective_settings()
        anchor = settings.billing_cycle_anchor_day or None
        today = timezone.localdate()
        start_dt = cycle_start_datetime(today, anchor_day=anchor)

        breakdown = TaskAttempt.objects.headless().filter(started_at__gte=start_dt).cost_breakdown()
        report = CostReport.build(
            breakdown,
            credit_usd=settings.sdk_monthly_credit_usd,
            cycle_start_date=cycle_start(today, anchor_day=anchor),
            today=today,
        )

        if json_output:
            return json.dumps(
                {
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
                },
            )
        return "\n".join(report.render_lines())
