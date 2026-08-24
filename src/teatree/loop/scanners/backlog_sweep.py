"""Periodic backlog-sweep scanner — #2419, #4344.

Companion to the ``sweeping-tickets`` skill: the loop fires a daily
``backlog_sweep`` task that GROUPS the issue tracker — every related ticket
bundled into an existing host, so the fixed per-ticket cost of a delivery
cycle is paid once for the bundle rather than once per row — without
depending on an external cron. The scanner is one of the periodic
task-queuing family that share
:class:`teatree.loop.scanners.phase_cadence.PhaseCadence`, and stamps two
safety properties onto every task it queues:

* **Group-first, close nothing for real.** Backlog size multiplies delivery
    cost, but the ideas in those rows are not the problem — their packaging
    is. So the directive's default posture is aggressive grouping, and no
    verdict discards content: a member's substance moves into its host
    (``t3 <overlay> ticket fold``) and is proved to have landed
    (``fold-check``) before its standalone row is retired.
* **Ask-gate in the directive.** The queued task carries an ASK-GATE
    marker so the dispatched sweep records fold proposals and surfaces the
    batch for explicit approval — it never mass-closes or mass-folds
    unattended, and every retirement routes through the gated
    ``ticket bulk-close`` command.

Other invariants mirror the family:

* **Single trigger.** Only a cadence (``backlog_sweep_cadence_hours``,
    default 24h = daily). A fixed-rate platform behaviour, not coupled
    to delivery velocity.
* **Overlay anchor is injected, not baked.** A core scanner that does not
    know any overlay's name; the wiring layer resolves the active core
    overlay via :func:`teatree.config.discover_active_overlay` and passes
    the result as the ``overlay_name`` constructor kwarg.
* **Same dedup contract.** A pending or claimed ``backlog_sweep`` task
    acts as the lock — completion (or failure) unlocks the next cadence
    window. No new model fields; the most recent task's
    ``Session.started_at`` is the "last run" timestamp.
"""

from dataclasses import dataclass

from django.utils import timezone

from teatree.core.modelkit.phases import BACKLOG_SWEEP_PHASE
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.phase_cadence import PhaseCadence

#: The default posture every queued sweep carries, ask-gate or not: group hard, discard
#: nothing. The substrings are load-bearing — they are the channel the dispatched skill
#: reads, so a run with no extra flags still groups and still closes nothing for real.
_GROUP_DIRECTIVE = (
    "GROUP-FIRST: grouping is the DEFAULT, not an opt-in — bundle every related ticket "
    "into an EXISTING host (never mint an umbrella), and a host MAY carry several "
    "unrelated small things when they share a module, seam or test file. "
    "CLOSE NOTHING FOR REAL: no verdict discards an idea — including an already-shipped "
    "one, which folds as content rather than closing. Every reduction is a FOLD: move the "
    "member's body into the host with `t3 <overlay> ticket fold`, re-read the host and "
    "prove it landed with `t3 <overlay> ticket fold-check`, and only then retire the "
    "standalone row"
)


@dataclass(slots=True)
class BacklogSweepScanner:
    """Queue a periodic ``backlog_sweep`` task for the active core overlay.

    Configuration fields are passed explicitly (rather than read from a
    global at scan time) so test setup is deterministic and the wiring
    layer is the single place that resolves
    :class:`teatree.config.UserSettings` and
    :func:`teatree.config.discover_active_overlay` to scanner kwargs. The
    on/off decision lives at the wiring layer (``backlog_sweep_disabled``
    in core config, and the ``backlog_sweep`` ``Loop`` row behind it); the
    scanner itself always scans when invoked.

    ``overlay_name`` is the resolved overlay-anchor identity for the
    placeholder ticket. The scanner never reads or assumes the name — it
    stamps whatever value the wiring layer hands it. The canonical default
    in production is ``"t3-teatree"``.

    ``require_approval`` is the ask-gate flag, resolved from
    ``ask_before_backlog_sweep_closes`` at the wiring layer. When true
    (the default), the queued task's directive instructs the dispatched
    skill to record each fold proposal and surface the batch for explicit
    user approval — it must NOT mass-close unattended. The scanner never
    touches an issue itself; this flag is the contract it stamps onto the
    task so the skill cannot silently fall back to bulk closing.
    """

    overlay_name: str
    skill: str = "sweeping-tickets"
    cadence_hours: int = 24
    require_approval: bool = True
    name: str = "backlog_sweep"

    def scan(self) -> list[ScanSignal]:
        cadence = PhaseCadence(self.overlay_name, phase=BACKLOG_SWEEP_PHASE, cadence_hours=self.cadence_hours)
        if cadence.in_flight_exists():
            return []

        trigger = cadence.evaluate_trigger(now=timezone.now(), last_run_at=cadence.last_run_at())
        if trigger is None:
            return []

        task = cadence.queue_task(
            placeholder_issue_url=f"backlog-sweep://{self.overlay_name}",
            agent_id=f"backlog-sweep-{self.overlay_name}",
            execution_reason=self._execution_reason(trigger),
            log_label="BacklogSweepScanner",
        )
        if task is None:
            return []
        return [
            ScanSignal(
                kind="backlog_sweep.queued",
                summary=f"backlog-sweep queued for {self.overlay_name} (trigger: {trigger})",
                payload={
                    "overlay": self.overlay_name,
                    "skill": self.skill,
                    "phase": BACKLOG_SWEEP_PHASE,
                    "task_id": task.pk,
                    "trigger": trigger,
                    "require_approval": self.require_approval,
                },
            ),
        ]

    def _execution_reason(self, trigger: str) -> str:
        """Build the dispatcher directive: the group-first posture, plus the ask-gate.

        :data:`_GROUP_DIRECTIVE` is unconditional — the sweep's default path
        groups and performs zero real closures whatever the ask-gate says.
        When ``require_approval`` is on (the default), the directive
        additionally requires each fold proposal to be surfaced for user
        approval, and routes every standalone retirement through the gated
        ``ticket bulk-close`` command so the no-bulk-close gate
        (:mod:`teatree.core.gates.bulk_close_gate`) applies to the autonomous
        path exactly as it does to a manual CLI one.
        """
        base = f"Periodic backlog-sweep triage ({trigger}) via skill: {self.skill} | {_GROUP_DIRECTIVE}"
        if self.require_approval:
            return (
                f"{base} | ASK-GATE: do NOT mass-close issues unattended — record each fold "
                "proposal with its citation and surface the batch for explicit user approval; "
                "a standalone row is retired only after its fold is verified, and that "
                "retirement MUST go through `t3 <overlay> ticket bulk-close --ids <ids> --confirm <ids>` "
                "(which enforces the no-bulk-close gate) — never a raw per-item `ticket ignore` loop "
                "(#2419, #1931, #4344)"
            )
        return base


__all__ = [
    "BACKLOG_SWEEP_PHASE",
    "BacklogSweepScanner",
]
