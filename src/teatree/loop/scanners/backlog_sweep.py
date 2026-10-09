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

* **Two triggers.** The ``backlog_sweep`` ``Loop`` row's own daily cadence,
    and a dream pass that left gaps on the umbrella host's pending ledger
    (:meth:`BacklogSweepScanner.scan_dream_gaps`); an empty ledger triggers nothing.
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
from teatree.core.models.consolidated_memory import ConsolidatedMemory
from teatree.core.models.dream_gap_ledger import pending_entries, umbrella_ticket
from teatree.core.models.types import DreamGapEntry
from teatree.dream_constants import DREAM_BATCH_MANIFEST_HEADER
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


#: What a sweep does with the gaps a dream pass left pending; every command named here must resolve.
DREAM_GAP_DIRECTIVE = (
    "Group them like tickets — fold each into the best EXISTING open host (oldest / most-discussed covering "
    "its scope, never a new row) with `t3 <overlay> ticket attach-gaps <host-ticket-id> --sweep-run-id <run> "
    '--manifest \'<json list of gap keys or {"gap_key": ..., "theme": ...} objects>\'`; a gap no host fits goes '
    "to the umbrella ticket itself. Pending:"
)


def _gap_detail_lines(row: ConsolidatedMemory | None) -> list[str]:
    if row is None:
        return []
    fields = (("Rule", row.rule), ("Evidence", row.verified_citation), ("Fix in", row.durable_destination))
    return [f"    {label}: {' '.join(value.split())}" for label, value in fields if value.strip()]


#: The Rule-4 half of #162: the sweep's changed-ticket count must be MEASURED. A number the
#: sweeping agent types into its envelope cannot distinguish a clean backlog from a skipped
#: sweep, so the run row is the measurement and the envelope only names it — enforced by
#: :mod:`teatree.agents.ticket_sweep_recorder`, stated here so the dispatched sweep opens the
#: run BEFORE it starts writing rather than discovering the requirement at completion.
_RUN_EVIDENCE_DIRECTIVE = (
    "MEASURED RUN: open the sweep run FIRST with `t3 <overlay> ticket sweep-begin --source loop`, "
    "pass `--sweep-run-id` to every mutation, close it with `ticket sweep-finish` even when nothing "
    'changed, and end the result envelope with `"ticket_sweep": {"run_id": "<the id>"}` — a '
    "backlog_sweep task that names no finished run is refused (#162 Rule 4)"
)


@dataclass(slots=True)
class BacklogSweepScanner:
    """Queue a periodic ``backlog_sweep`` task for the active core overlay.

    Configuration fields are passed explicitly (rather than read from a
    global at scan time) so test setup is deterministic and the wiring
    layer is the single place that resolves
    :class:`teatree.config.UserSettings` and
    :func:`teatree.config.discover_active_overlay` to scanner kwargs. The
    on/off decision is the ``backlog_sweep`` ``Loop`` row and the active preset; the
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
    require_approval: bool = True
    dream_umbrella_url: str = ""
    name: str = "backlog_sweep"

    def scan(self) -> list[ScanSignal]:
        cadence = PhaseCadence(self.overlay_name, phase=BACKLOG_SWEEP_PHASE)
        if cadence.in_flight_exists():
            return []

        trigger = cadence.evaluate_trigger(now=timezone.now(), last_run_at=cadence.last_run_at())
        if trigger is None:
            return []
        return self._queue(cadence, trigger, self._pending_dream_gaps())

    def scan_dream_gaps(self) -> list[ScanSignal]:
        """Queue a sweep for the gaps a dream pass left pending; an empty ledger queues nothing."""
        cadence = PhaseCadence(self.overlay_name, phase=BACKLOG_SWEEP_PHASE)
        pending = self._pending_dream_gaps()
        if not pending or cadence.in_flight_exists():
            return []
        return self._queue(cadence, "dream-gaps", pending)

    def _pending_dream_gaps(self) -> list[DreamGapEntry]:
        umbrella = umbrella_ticket(self.dream_umbrella_url) if self.dream_umbrella_url else None
        return pending_entries(umbrella) if umbrella is not None else []

    def _queue(self, cadence: PhaseCadence, trigger: str, pending: list[DreamGapEntry]) -> list[ScanSignal]:
        task = cadence.queue_task(
            placeholder_issue_url=f"backlog-sweep://{self.overlay_name}",
            agent_id=f"backlog-sweep-{self.overlay_name}",
            execution_reason=self._execution_reason(trigger) + self._dream_gap_section(pending),
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

        :data:`_GROUP_DIRECTIVE` and :data:`_RUN_EVIDENCE_DIRECTIVE` are both
        unconditional — the sweep's default path groups, performs zero real
        closures, and is measured, whatever the ask-gate says.
        When ``require_approval`` is on (the default), the directive
        additionally requires each fold proposal to be surfaced for user
        approval, and routes every standalone retirement through the gated
        ``ticket bulk-close`` command so the no-bulk-close gate
        (:mod:`teatree.core.gates.bulk_close_gate`) applies to the autonomous
        path exactly as it does to a manual CLI one.
        """
        base = (
            f"Periodic backlog-sweep triage ({trigger}) via skill: {self.skill} "
            f"| {_GROUP_DIRECTIVE} | {_RUN_EVIDENCE_DIRECTIVE}"
        )
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

    def _dream_gap_section(self, pending: list[DreamGapEntry]) -> str:
        """Name every gap a dream pass left pending, and the one verb that folds them into a host."""
        if not pending:
            return ""
        rows = ConsolidatedMemory.objects.filter(cluster_key__in={entry.get("cluster_key") for entry in pending})
        by_cluster = {row.cluster_key: row for row in rows}
        lines = []
        for entry in pending:
            lines.append(f"- {entry.get('gap_key', '')}: {entry.get('title', '')}")
            lines.extend(_gap_detail_lines(by_cluster.get(entry.get("cluster_key", ""))))
        detail_lines = "\n".join(lines)
        return (
            f" | DREAM GAPS: {len(pending)} gap(s) wait on {self.dream_umbrella_url}. "
            f"{DREAM_GAP_DIRECTIVE}\n{DREAM_BATCH_MANIFEST_HEADER}\n{detail_lines}"
        )


__all__ = [
    "BACKLOG_SWEEP_PHASE",
    "DREAM_GAP_DIRECTIVE",
    "BacklogSweepScanner",
]
