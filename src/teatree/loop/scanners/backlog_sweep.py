"""Periodic backlog-sweep scanner — #2419.

Companion to the ``sweeping-tickets`` skill: once the sweep's verdicts prove
trustworthy, the loop fires a low-frequency ``backlog_sweep`` task that
consolidates the issue tracker (shipped / consolidate-into-epic /
regressive / still-standalone against current ``main``) without depending
on an external cron. The scanner is one of the periodic task-queuing family
that share :class:`teatree.loop.scanners.phase_cadence.PhaseCadence`, and bakes
in two safety properties from day one because the sweep is destructive-capable
(it can propose closing issues):

* **Default-OFF.** Unlike the always-on news/eval scanners, the kill
    switch (``backlog_sweep_disabled``) defaults *on* at the wiring layer,
    so the scanner is inert until the user opts in. This module always
    scans when invoked — the on/off decision lives at the wiring layer
    (``teatree.loop.global_scanner_factories._backlog_sweep_scanner``).
* **Ask-gate in the directive.** The queued task carries an ASK-GATE
    marker so the dispatched sweep records close/fold proposals and
    surfaces the batch for explicit approval — it never mass-closes or
    mass-folds unattended. Only the high-confidence shipped-by-merged-PR
    class auto-closes (the skill's own discipline).

Other invariants mirror the family:

* **Single trigger.** Only a cadence (``backlog_sweep_cadence_hours``,
    default 168h = weekly). A fixed-rate platform behaviour, not coupled
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


@dataclass(slots=True)
class BacklogSweepScanner:
    """Queue a periodic ``backlog_sweep`` task for the active core overlay.

    Configuration fields are passed explicitly (rather than read from a
    global at scan time) so test setup is deterministic and the wiring
    layer is the single place that resolves
    :class:`teatree.config.UserSettings` and
    :func:`teatree.config.discover_active_overlay` to scanner kwargs. The
    on/off decision lives at the wiring layer (``backlog_sweep_disabled``
    in core config, defaulting ON); the scanner itself always scans when
    invoked.

    ``overlay_name`` is the resolved overlay-anchor identity for the
    placeholder ticket. The scanner never reads or assumes the name — it
    stamps whatever value the wiring layer hands it. The canonical default
    in production is ``"t3-teatree"``.

    ``require_approval`` is the ask-gate flag, resolved from
    ``ask_before_backlog_sweep_closes`` at the wiring layer. When true
    (the default), the queued task's directive instructs the dispatched
    skill to record each close proposal and surface the batch for explicit
    user approval — it must NOT mass-close unattended. The scanner never
    closes issues itself; this flag is the contract it stamps onto the
    task so the skill cannot silently fall back to bulk closing.
    """

    overlay_name: str
    skill: str = "sweeping-tickets"
    cadence_hours: int = 168
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
        """Build the dispatcher directive, embedding the ask-gate contract.

        When ``require_approval`` is on (the default), the directive
        carries an explicit instruction that the skill must record each
        close proposal and surface the batch for user approval — it must
        NOT mass-close issues unattended. It also routes the one
        auto-closable class through the gated ``ticket bulk-close`` command
        so the no-bulk-close gate (:mod:`teatree.core.gates.bulk_close_gate`)
        applies to the autonomous close path exactly as it does to a manual
        CLI one. The marker substrings are load-bearing: they are the
        channel the dispatched skill reads to know the gate is active.
        """
        base = f"Periodic backlog-sweep triage ({trigger}) via skill: {self.skill}"
        if self.require_approval:
            return (
                f"{base} | ASK-GATE: do NOT mass-close issues unattended — record each close "
                "proposal with its citation and surface the batch for explicit user approval; "
                "only the high-confidence merged-PR-superseded class auto-closes, and that "
                "auto-close MUST go through `t3 <overlay> ticket bulk-close --ids <ids> --confirm <ids>` "
                "(which enforces the no-bulk-close gate) — never a raw per-item `ticket ignore` loop "
                "(#2419, #1931)"
            )
        return base


__all__ = [
    "BACKLOG_SWEEP_PHASE",
    "BacklogSweepScanner",
]
