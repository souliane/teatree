"""Queue periodic local eval tasks on the Loop row's cadence."""

from collections.abc import Callable
from dataclasses import dataclass

from django.utils import timezone

from teatree.core.modelkit.phases import EVAL_LOCAL_PHASE
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.phase_cadence import PhaseCadence
from teatree.token_report import EvalReserveDecision, local_eval_reserve_decision


@dataclass(slots=True)
class EvalLocalScanner:
    """Queue a task when the Loop row is due and the factory account has reserve."""

    overlay_name: str
    name: str = "eval_local"
    reserve_met: Callable[[], EvalReserveDecision] = local_eval_reserve_decision

    def scan(self) -> list[ScanSignal]:
        cadence = PhaseCadence(self.overlay_name, phase=EVAL_LOCAL_PHASE)
        if cadence.in_flight_exists():
            return []

        trigger = cadence.evaluate_trigger(now=timezone.now(), last_run_at=cadence.last_run_at())
        if trigger is None:
            return []
        reserve = self.reserve_met()
        if reserve.stale and not reserve.met:
            return [
                ScanSignal(
                    kind="eval_local.parked",
                    summary=f"local eval parked for {self.overlay_name}: {reserve.reason}",
                    payload={"overlay": self.overlay_name, "reason": reserve.reason},
                )
            ]
        if not reserve.met:
            return [
                ScanSignal(
                    kind="eval_local.held",
                    summary=f"local eval held for {self.overlay_name}: {reserve.reason or 'reserve unmet'}",
                    payload={"overlay": self.overlay_name, "reason": reserve.reason or "reserve unmet"},
                )
            ]

        task = cadence.queue_task(
            placeholder_issue_url=f"eval-local://{self.overlay_name}",
            agent_id=f"eval-local-{self.overlay_name}",
            execution_reason=f"Periodic local behavioral eval ({trigger}) on the factory subscription account",
            log_label="EvalLocalScanner",
        )
        if task is None:
            return []
        return [
            ScanSignal(
                kind="eval_local.queued",
                summary=f"local eval queued for {self.overlay_name} (trigger: {trigger})",
                payload={
                    "overlay": self.overlay_name,
                    "phase": EVAL_LOCAL_PHASE,
                    "task_id": task.pk,
                    "trigger": trigger,
                },
            ),
        ]


__all__ = [
    "EVAL_LOCAL_PHASE",
    "EvalLocalScanner",
]
