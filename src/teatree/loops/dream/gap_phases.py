"""The three dream phases that drive a gap into this pass's promotion batch (#2663, #4176).

Compliance escalation (3c), the automatable-ask promoter (3d), and Pass-2 core-gap
memory promotion share one shape: resolve the backlog code host, delegate to the module
that owns the work, and fault-isolate the whole thing to a WARN clause so a phase failure
never aborts the pass. Composed onto the command through ``backlog_host_resolver``,
keeping the command a wiring layer.

Compliance and automatable asks run on every pass. Memory promotion keeps its
``memory_promote`` toggle, gated with the ``not force_all_phases and not <toggle>()``
OR-idiom: ``force_all_phases`` is the ``--full`` alias for one manual pass, which the
nightly ``tick`` cannot set, so no gate ANDs on it (#4176).

Each phase COLLECTS its gaps into the pass's shared
:class:`~teatree.loops.dream.batch_promote.PromotionBatch` rather than scheduling its
own ticket; the batch queues them for the backlog sweep and mints none (#4776).
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.core.models.dream_gap_ledger import dream_umbrella_url

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.loops.dream.batch_promote import PromotionBatch
    from teatree.loops.dream.replay import ConsolidationExtract

BacklogHostResolver = Callable[[], "tuple[CodeHostBackend | None, str]"]


@dataclass(frozen=True, slots=True)
class GapPromotionPhases:
    """The pass's gap-promoting phases, wired to one backlog-host resolver."""

    backlog_host_resolver: BacklogHostResolver

    def run_compliance(self, *, extract: "ConsolidationExtract | None", dry_run: bool, batch: "PromotionBatch") -> str:
        """Phase 3c — MEASURE compliance, then ESCALATE each recurrence, every pass (never raises).

        Measurement is the root KPI: it reuses the extract the engine already built (no
        re-enumeration) and PERSISTS a snapshot (never files). Escalation queues each
        recurrence onto the standing umbrella through this pass's promotion batch.
        """
        if extract is None:
            return ""
        try:
            from teatree.loops.dream import compliance  # noqa: PLC0415 — deferred: keeps command import light

            measurement = compliance.run_compliance_measurement(extract=extract, dry_run=dry_run)
            summary = measurement.summary
            host, _repo = self.backlog_host_resolver()
            summary += compliance.run_compliance_escalation(
                snapshot=measurement.snapshot,
                findings=measurement.findings,
                host=host,
                dry_run=dry_run,
                batch=batch,
            )
        except Exception as exc:  # noqa: BLE001 — a compliance-phase failure degrades to a WARN clause, never aborts the dream
            return f"; WARN compliance phase raised: {type(exc).__name__}: {exc}"
        return summary

    def run_automation_asks(
        self,
        *,
        extract: "ConsolidationExtract | None",
        dry_run: bool,
        batch: "PromotionBatch",
    ) -> str:
        """Phase 3d — promote recurring automatable user asks to a fix-and-merge (#2663; never raises).

        The "improve-with-new-stuff" sibling of the compliance accountant, run on every
        pass. It QUEUES each grounded ask for the backlog sweep. The detect → classify →
        promote work lives in
        :func:`teatree.loops.dream.automation_ask.run_automation_asks_phase`; this reuses
        the bounded extract the engine already built (for the grounding guard).
        """
        if extract is None:
            return ""
        try:
            from teatree.loops.dream import automation_ask  # noqa: PLC0415 — deferred: phase-only import

            host, _repo = self.backlog_host_resolver()
            if host is None:
                return "; WARN automatable-ask promotion skipped — no teatree code host resolved"
            return automation_ask.run_automation_asks_phase(
                extract, umbrella_url=dream_umbrella_url(), dry_run=dry_run, batch=batch
            )
        except Exception as exc:  # noqa: BLE001 — an automatable-ask-phase failure degrades to a WARN clause
            return f"; WARN automatable-ask phase raised: {type(exc).__name__}: {exc}"

    def run_memory_promotion(self, *, dry_run: bool, force_all_phases: bool, batch: "PromotionBatch") -> str:
        """Pass 2 — collect each core gap into this pass's promotion batch (#2663, #4776).

        Runs only when the default-ON ``memory_promote`` toggle admits it (#4685, #4776).
        Resolves the teatree backlog code host, triages
        every untriaged ``ConsolidatedMemory`` row, and COLLECTS each core-generic gap
        into *batch* (queued once, after every promoting phase has run — see
        ``batch_promote.promote_batch``). Then it RECONCILES gap-fix
        tickets scheduled under the OLD per-gap scheme (legacy in-flight tickets only —
        the batch reconcile is a separate pass-level step). A failure is reported in the
        summary line, never crashing the pass.
        """
        from teatree.loops.dream.loop import memory_promote_enabled  # noqa: PLC0415 — deferred: lazy command import

        if not force_all_phases and not memory_promote_enabled():
            return ""
        try:
            from teatree.loops.dream import batch_promote, promote_memory  # noqa: PLC0415 — lazy command import

            host, _repo = self.backlog_host_resolver()
            if host is None:
                return "; WARN memory promotion skipped — no teatree code host resolved"
            umbrella = dream_umbrella_url()
            promoted = promote_memory.file_core_gap_tickets(umbrella_url=umbrella, dry_run=dry_run, batch=batch)
            reconciled = [] if dry_run else batch_promote.reconcile_batches(host, umbrella_url=umbrella)
        except Exception as exc:  # noqa: BLE001 — a memory-promotion failure degrades to a WARN clause
            return f"; WARN memory promotion raised: {type(exc).__name__}: {exc}"
        new_fixes = sum(1 for o in promoted if o.filed)
        withheld = sum(1 for o in promoted if o.withheld)
        if not promoted and not reconciled:
            return ""
        summary = f"; promoted {new_fixes} core-gap fix(es), reconciled {len(reconciled)} merged"
        # A withheld gap shows in neither count, so without its own clause an
        # ungrounded or leak-scrubbed gap is silently invisible in the pass line.
        return f"{summary}, withheld {withheld}" if withheld else summary


__all__ = ["BacklogHostResolver", "GapPromotionPhases"]
