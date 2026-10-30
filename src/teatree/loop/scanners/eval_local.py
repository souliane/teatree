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
        trigger, signals = cadence.gate(
            now=timezone.now(), held_kind="eval_local.held", not_due_kind="eval_local.not_due"
        )
        if trigger is None:
            return signals
        reserve = self.reserve_met()
        if reserve.stale and not reserve.met:
            return cadence.hold(reserve.reason or "reserve reading stale", kind="eval_local.parked", retry=True)
        if not reserve.met:
            return cadence.hold(reserve.reason or "reserve unmet", kind="eval_local.held", retry=True)

        task = cadence.queue_task(
            placeholder_issue_url=f"eval-local://{self.overlay_name}",
            agent_id=f"eval-local-{self.overlay_name}",
            execution_reason=f"Periodic local behavioral eval ({trigger}) on the factory subscription account",
            log_label="EvalLocalScanner",
        )
        return cadence.handover(
            task,
            ScanSignal(
                kind="eval_local.queued",
                summary=f"local eval queued for {self.overlay_name} (trigger: {trigger})",
                payload={
                    "overlay": self.overlay_name,
                    "phase": EVAL_LOCAL_PHASE,
                    "task_id": task.pk if task is not None else None,
                    "trigger": trigger,
                },
            ),
            held_kind="eval_local.held",
        )


__all__ = [
    "EVAL_LOCAL_PHASE",
    "EvalLocalScanner",
]
