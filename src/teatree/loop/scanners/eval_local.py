"""Periodic local-eval scanner.

The user directive (2026-06-05): "AI evals should be run locally from
time to time, and in CI once a week." The CI half already exists
(``.github/workflows/ci.yml`` ``eval-weekly`` + ``scripts/eval/
first_pr_of_week.py``). This is the local half: the loop fires an
``eval_local`` task per cadence window (default 168h = weekly) so the
SCOPED eval suite runs locally without depending on an external cron.

The scanner is one of the periodic task-queuing family that share
:class:`teatree.loop.scanners.phase_cadence.PhaseCadence`:

* **Single trigger.** Only a cadence (``eval_local_cadence_hours``,
    default 168h). A fixed-rate platform behaviour, not coupled to
    delivery velocity.
* **Overlay anchor is injected, not baked.** A core scanner that does
    not know any overlay's name; the wiring layer
    (``teatree.loop.global_scanner_factories._eval_local_scanner``) resolves the
    active overlay via :func:`teatree.config.discover_active_overlay`.
* **Same dedup contract.** A pending or claimed ``eval_local`` task acts
    as the lock — completion (or failure) unlocks the next cadence
    window. No new model fields; the most recent task's
    ``Session.started_at`` is the "last run" timestamp.
* **Non-blocking.** ``scan()`` only writes the Task row and returns; the
    dispatcher routes it through the standard pending-task pipeline. The
    queued task's directive runs the local transcript runner (the same
    one ``t3 eval run`` defaults to — $0 extra, runs no model), so the
    long-running suite never blocks the tick.
"""

from dataclasses import dataclass

from django.utils import timezone

from teatree.core.modelkit.phases import EVAL_LOCAL_PHASE
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.phase_cadence import PhaseCadence


@dataclass(slots=True)
class EvalLocalScanner:
    """Queue a periodic ``eval_local`` task for the active core overlay.

    Configuration fields are passed explicitly (rather than read from a
    global at scan time) so test setup is deterministic and the wiring
    layer is the single place that resolves
    :class:`teatree.config.UserSettings` and
    :func:`teatree.config.discover_active_overlay` to scanner kwargs. The
    on/off decision lives at the wiring layer (``eval_local_disabled`` in
    core config); the scanner itself always scans when invoked.
    """

    overlay_name: str
    skill: str = "eval"
    cadence_hours: int = 168
    name: str = "eval_local"

    def scan(self) -> list[ScanSignal]:
        cadence = PhaseCadence(self.overlay_name, phase=EVAL_LOCAL_PHASE, cadence_hours=self.cadence_hours)
        if cadence.in_flight_exists():
            return []

        trigger = cadence.evaluate_trigger(now=timezone.now(), last_run_at=cadence.last_run_at())
        if trigger is None:
            return []

        task = cadence.queue_task(
            placeholder_issue_url=f"eval-local://{self.overlay_name}",
            agent_id=f"eval-local-{self.overlay_name}",
            execution_reason=self._execution_reason(trigger),
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
                    "skill": self.skill,
                    "phase": EVAL_LOCAL_PHASE,
                    "task_id": task.pk,
                    "trigger": trigger,
                },
            ),
        ]

    def _execution_reason(self, trigger: str) -> str:
        """Direct the SCOPED local run via the $0-extra transcript runner.

        The dispatched skill reads ``execution_reason``; the ``t3 eval
        run`` + ``transcript`` substrings are load-bearing — they tell
        the skill to run the same scoped, $0-extra path the user runs
        by hand (``t3 eval run`` defaults to the transcript backend),
        plus the deterministic ``pinned-regressions`` check.
        """
        return (
            f"Periodic local eval ({trigger}) via skill: {self.skill} | run the SCOPED suite locally with "
            "`t3 eval pinned-regressions` and `t3 eval run` "
            "(transcript backend, $0 extra)"
        )


__all__ = [
    "EVAL_LOCAL_PHASE",
    "EvalLocalScanner",
]
