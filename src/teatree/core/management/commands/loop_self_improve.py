"""``manage.py loop_self_improve`` — one schedule cycle of the self-improve monitor.

Mirrors the shape of ``loops_tick``: acquires a dedicated ``LoopLease``
(``loop-self-improve``) so a long self-improve cycle never blocks a fast
regular tick, defers when it does not hold the t3-master gate
(:mod:`teatree.core.gates.t3_master_gate`), runs the tier dispatcher, and prints
a one-line summary (or the JSON report when ``--json`` is passed).
"""

import datetime as dt
import logging
import os
from dataclasses import asdict
from typing import IO, TYPE_CHECKING, Annotated, Any, cast

import typer
from django_typer.management import TyperCommand

from teatree.core.gates.t3_master_gate import t3_master_verdict
from teatree.core.machine_output import emit
from teatree.core.modelkit.notify_policy import NotifyAudience
from teatree.core.notify_types import NotifyKind

if TYPE_CHECKING:
    from teatree.core.models.self_improve_firing import SelfImproveFiring
    from teatree.loop.self_improve.detectors.base import DetectorReport
    from teatree.loop.self_improve.schedule import TierResult

type ReportDict = dict[str, Any]
logger = logging.getLogger(__name__)


def _deliver_owner_alert(report: "DetectorReport", existing: "SelfImproveFiring | None") -> bool:
    """Use the verified bot→owner egress without a reverse domain dependency."""
    from teatree.core.notify import notify_user_outcome  # noqa: PLC0415 — keep command import light for CLI discovery
    from teatree.loop.self_improve.actions import (  # noqa: PLC0415 — defer domain action import until command execution
        format_slack_payload,
        owner_alert_key,
    )

    outcome = notify_user_outcome(
        format_slack_payload(report)["text"],
        kind=NotifyKind.INFO,
        idempotency_key=owner_alert_key(report, existing),
        audience=NotifyAudience.OWNER_ESCALATION,
    )
    if not outcome.sent:
        logger.warning("self-improve owner alert not delivered: %s (%s)", report.dedup_key, outcome.reason)
    return outcome.sent


def _result_to_dict(result: "TierResult") -> ReportDict:
    return {
        "tier": result.tier,
        "budget_ok": result.budget.ok,
        "budget_reason": result.budget.reason,
        "skipped": result.skipped,
        "report_count": len(result.reports),
        "action_count": len(result.actions),
        "degraded_scans": [{"detector": detector, "reason": reason} for detector, reason in result.degraded_scans],
        "reports": [asdict(r) for r in result.reports],
        "actions": [
            {
                "rung": a.rung,
                "firing_id": a.firing.pk,
                "detector": a.firing.detector,
                "dedup_key": a.firing.dedup_key,
                "slack_capped": a.slack_capped,
                "auto_fix_executed": a.auto_fix_executed,
            }
            for a in result.actions
        ],
    }


class Command(TyperCommand):
    help = "Run one schedule cycle of the self-improving monitor."

    def handle(
        self,
        *,
        tier: Annotated[
            str,
            typer.Option(
                "--tier",
                help="Cost tier: cheap|all (default: cheap). medium/expensive have no detectors and are refused.",
            ),
        ] = "cheap",
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the cycle report as JSON."),
        ] = False,
    ) -> None:
        from teatree.core.models import LoopLease  # noqa: PLC0415 — deferred: ORM import needs the app registry
        from teatree.core.overlay_loader import get_overlay, overlay_name_of  # noqa: PLC0415 — lazy command import
        from teatree.loop.phases.render import self_improve_rerender  # noqa: PLC0415 — deferred: lazy command import
        from teatree.loop.self_improve.schedule import (  # noqa: PLC0415 — deferred: keeps command import light
            DeliveryRoutes,
            UnimplementedTierError,
            require_implemented_tier,
            run_tier,
        )

        out = cast("IO[str]", self.stdout)
        err = cast("IO[str]", self.stderr)
        try:
            require_implemented_tier(tier)
        except UnimplementedTierError as exc:
            err.write(f"REFUSE  {exc}\n")
            raise SystemExit(2) from exc
        verdict = t3_master_verdict()
        if not verdict.may_run:
            now = dt.datetime.now(tz=dt.UTC)
            emit(
                {
                    "tier": tier,
                    "skipped": True,
                    "skipped_reason": verdict.outcome.value,
                    "owner_session": verdict.owner_session,
                    "started_at": now.isoformat(),
                },
                json_output=json_output,
                out=out,
                err=err,
                human=verdict.skip_message("self-improve"),
            )
            return

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire("loop-self-improve", owner=owner):
            now = dt.datetime.now(tz=dt.UTC)
            emit(
                {
                    "tier": tier,
                    "skipped": True,
                    "skipped_reason": "another self-improve cycle is already running",
                    "started_at": now.isoformat(),
                },
                json_output=json_output,
                out=out,
                err=err,
                human="SKIP  loop-self-improve lease held — another cycle is running.",
            )
            return
        try:
            overlay_name = overlay_name_of(get_overlay())
            result = run_tier(
                tier,
                auto_fix_callable=self_improve_rerender,
                delivery=DeliveryRoutes(owner_alert=_deliver_owner_alert, overlay_name=overlay_name),
            )
        finally:
            LoopLease.objects.release("loop-self-improve", owner=owner)

        report = _result_to_dict(result)
        if result.degraded_scans:
            unknown = ",".join(f"{detector}:{reason}" for detector, reason in result.degraded_scans)
            budget_reason = f" budget={result.budget.reason}" if result.skipped else ""
            human = (
                f"DEGRADED tier={result.tier} reports={len(result.reports)} "
                f"actions={len(result.actions)} unknown_scans={unknown}{budget_reason}"
            )
        elif result.skipped and result.actions:
            human = (
                f"DEGRADED budget gate: {result.budget.reason}; "
                f"safe reports={len(result.reports)} actions={len(result.actions)}"
            )
        elif result.skipped:
            human = f"SKIP  budget gate: {result.budget.reason}"
        else:
            human = f"OK    tier={result.tier} reports={len(result.reports)} actions={len(result.actions)}"
        emit(report, json_output=json_output, out=out, err=err, human=human)
