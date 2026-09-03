"""The three dream phases that drive a gap to a scheduled fix (#2663, #4176).

Compliance escalation (3c), the automatable-ask promoter (3d), and Pass-2 core-gap
memory promotion share one shape: gate on the phase's own config toggle, resolve the
backlog code host, delegate to the module that owns the work, and fault-isolate the
whole thing to a WARN clause so a phase failure never aborts the pass. Composed onto the
command through ``backlog_host_resolver`` exactly as
:class:`teatree.loops.dream.phase_runner.MemoryPhaseRunner` is, keeping the command a
wiring layer.

``force_all_phases`` is the ``--full`` convenience alias for one manual pass. The nightly
``tick`` cannot set it, so no gate here ANDs on it — a phase gated that way is dead on
the cron path however its own toggle is configured (#4176). Each gate is its own toggle
alone, or the ``not force_all_phases and not <toggle>()`` OR-idiom.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from teatree.loops.dream.pass_config import PromotionBudget

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.loops.dream.replay import ConsolidationExtract

BacklogHostResolver = Callable[[], "tuple[CodeHostBackend | None, str]"]


@dataclass(frozen=True, slots=True)
class GapPromotionPhases:
    """The pass's gap-promoting phases, wired to one backlog-host resolver."""

    backlog_host_resolver: BacklogHostResolver

    def run_compliance(
        self, *, extract: "ConsolidationExtract | None", dry_run: bool, budget: PromotionBudget | None = None
    ) -> str:
        """Phase 3c — MEASURE compliance every pass, ESCALATE on the escalate toggle (never raises).

        Measurement is the root KPI: it runs on EVERY pass when the default-ON
        ``compliance_measure`` toggle admits it, reusing the extract the engine already
        built (no re-enumeration) and PERSISTING a snapshot (never files). Escalation —
        the default-OFF ticket-filing half — runs when the ``compliance_escalate`` toggle
        admits it, driving each recurrence onto the standing umbrella. That toggle used to
        be ANDed with ``--full``, which the nightly ``tick`` can never set, so the toggle
        was dead on the cron path (#4176).
        """
        if extract is None:
            return ""
        try:
            from teatree.loops.dream import compliance  # noqa: PLC0415 — deferred: keeps command import light
            from teatree.loops.dream.loop import (  # noqa: PLC0415 — deferred: breaks the loop->command import cycle
                compliance_escalate_enabled,
                compliance_measure_enabled,
            )

            if not compliance_measure_enabled():
                return ""
            measurement = compliance.run_compliance_measurement(extract=extract, dry_run=dry_run)
            summary = measurement.summary
            if compliance_escalate_enabled():
                host, _repo = self.backlog_host_resolver()
                summary += compliance.run_compliance_escalation(
                    snapshot=measurement.snapshot,
                    findings=measurement.findings,
                    host=host,
                    dry_run=dry_run,
                    budget=budget,
                )
        except Exception as exc:  # noqa: BLE001 — a compliance-phase failure degrades to a WARN clause, never aborts the dream
            return f"; WARN compliance phase raised: {type(exc).__name__}: {exc}"
        return summary

    def run_automation_asks(
        self,
        *,
        extract: "ConsolidationExtract | None",
        dry_run: bool,
        force_all_phases: bool,
        budget: PromotionBudget | None = None,
    ) -> str:
        """Phase 3d — promote recurring automatable user asks to a fix-and-merge (#2663; never raises).

        The "improve-with-new-stuff" sibling of the compliance accountant, gated by the
        OR-idiom: it runs when the ``--full`` pipeline is requested OR the default-OFF
        ``automation_asks`` toggle is on (env / toml). It PROMOTES fixes (a checkbox +
        scheduled coding task under the standing umbrella). The detect → classify →
        promote work lives in
        :func:`teatree.loops.dream.automation_ask.run_automation_asks_phase`; this reuses
        the bounded extract the engine already built (for the grounding guard).
        """
        from teatree.loops.dream.loop import automation_asks_enabled  # noqa: PLC0415 — deferred: lazy command import

        if not force_all_phases and not automation_asks_enabled():
            return ""
        if extract is None:
            return ""
        try:
            from teatree.loops.dream import automation_ask  # noqa: PLC0415 — deferred: phase-only import

            host, _repo = self.backlog_host_resolver()
            if host is None:
                return "; WARN automatable-ask promotion skipped — no teatree code host resolved"
            return automation_ask.run_automation_asks_phase(
                extract, host, umbrella_url=_umbrella_url(), dry_run=dry_run, budget=budget
            )
        except Exception as exc:  # noqa: BLE001 — an automatable-ask-phase failure degrades to a WARN clause
            return f"; WARN automatable-ask phase raised: {type(exc).__name__}: {exc}"

    def run_memory_promotion(
        self, *, dry_run: bool, force_all_phases: bool = False, budget: PromotionBudget | None = None
    ) -> str:
        """Pass 2 — drive each core gap to a fix-and-merge under the umbrella (#2663).

        Runs only when the default-OFF ``memory_promote`` toggle is on, because it
        schedules fixes. Resolves the teatree backlog code host, triages every
        untriaged ``ConsolidatedMemory`` row, and PROMOTES each core-generic gap to a
        fix: a checkbox is upserted under the standing umbrella issue and a coding task
        is scheduled (instead of a fresh ``needs-triage`` issue that piles up). Then it
        RECONCILES — every gap whose fix Ticket merged has its umbrella checkbox checked
        and its memory retired. A failure is reported in the summary line, never
        crashing the pass.
        """
        from teatree.loops.dream.loop import memory_promote_enabled  # noqa: PLC0415 — deferred: lazy command import

        if not force_all_phases and not memory_promote_enabled():
            return ""
        try:
            from teatree.loops.dream import promote_memory, umbrella_ledger  # noqa: PLC0415 — lazy command import

            host, _repo = self.backlog_host_resolver()
            if host is None:
                return "; WARN memory promotion skipped — no teatree code host resolved"
            umbrella = promote_memory.UMBRELLA_ISSUE_URL
            promoted = promote_memory.file_core_gap_tickets(host, umbrella_url=umbrella, dry_run=dry_run, budget=budget)
            reconciled = [] if dry_run else umbrella_ledger.reconcile_merged_gaps(host, umbrella_url=umbrella)
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


def _umbrella_url() -> str:
    """The standing umbrella issue every grounded dream gap rides (#2663)."""
    from teatree.loops.dream.promote_memory import UMBRELLA_ISSUE_URL  # noqa: PLC0415 — lazy command import

    return UMBRELLA_ISSUE_URL


__all__ = ["BacklogHostResolver", "GapPromotionPhases"]
