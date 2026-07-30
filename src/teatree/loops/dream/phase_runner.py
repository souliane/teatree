"""File-side memory-phase runner for the dream cron (#1933 §6, #2545, #2723).

The ``dream`` management command owns the cron MECHANICS (lease, cadence,
marker). The file-side memory phases — cross-link (4), merge (4b), re-index (5),
decay (6) — plus the §4 acceptance gates that grade them are a cohesive concern
of their own, extracted here as a composed :class:`MemoryPhaseRunner` so the
command stays focused on the cron loop.

Every phase runs LIVE by default behind its own ``[loops.dream]`` / ``T3_DREAM_*``
kill-switch and each in its own try/except, so one phase (or one memory dir)
failing never crashes the tick or stops the other phases. The runner takes a
``backlog_host_resolver`` so the binding-reconciliation ticket path (Decision-3,
#2723) reaches the same teatree backlog host the command resolves, without the
runner importing the command.
"""

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

from django.apps import apps

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.core.models import ConsolidatedMemory
    from teatree.loops.dream.decay import ArchivedMemory
    from teatree.loops.dream.gates import MemorySnapshot
    from teatree.loops.dream.merge import BindingConflict

#: Resolve the teatree backlog code host + repo slug for ticket filing.
BacklogHostResolver = Callable[[], "tuple[CodeHostBackend | None, str]"]


class _PhaseRunnerOne(Protocol):
    """A single-dir phase runner: work one memory dir, return the work count."""

    def __call__(self, d: Path, *, dry_run: bool) -> int: ...


class MemoryPhaseRunner:
    """Runs the file-side dream phases (4 / 4b / 5 / 6) and the §4 acceptance gates.

    Composed by the ``dream`` command; the binding-reconciliation filer reaches the
    command's backlog host through the injected *backlog_host_resolver*.
    """

    def __init__(self, *, backlog_host_resolver: BacklogHostResolver) -> None:
        self._backlog_host_resolver = backlog_host_resolver

    def run_memory_phases(self, *, dry_run: bool) -> str:
        """Run phases 4-6 over every discovered memory dir, then grade any mutation.

        The quiet-night path (0 transcript members): the SAME budget-aware phase
        machinery the gated path uses (:meth:`_run_phases`, which runs decay with the
        #2723 ``BudgetTier`` policy), not a policy-less near-copy. Because those phases
        MUTATE the memory files ungated, the §4 acceptance gates now run on ANY
        non-dry-run file mutation (F6.2) — a quiet-night decay/merge that loses a lesson
        is caught rather than silently laundered. The gate verdict is informational
        here (the command marks the pass attempted regardless); its clause rides the
        returned summary. A pass that mutated nothing, or a dry run, is left ungraded.
        """
        from teatree.loops.dream import gates  # noqa: PLC0415 — deferred: loaded at tick time, not import
        from teatree.memory_audit import discover_memory_dirs  # noqa: PLC0415 — deferred: loaded at tick time

        memory_dirs = discover_memory_dirs()
        if not memory_dirs:
            return ""
        before = {d: gates.snapshot_memory_dir(d) for d in memory_dirs}
        schema_before = self._ledger_schema_count()
        phase_summary, archived_by_dir, maintenance_performed = self._run_phases(memory_dirs, dry_run=dry_run)
        if dry_run or not maintenance_performed:
            return phase_summary
        schema_after = self._ledger_schema_count()
        _all_passed, gate_summary = self._grade_dirs(
            before,
            memory_dirs,
            archived_by_dir=archived_by_dir,
            schema_before=schema_before,
            schema_after=schema_after,
            clusters_recorded=0,
            maintenance_performed=maintenance_performed,
            dry_run=dry_run,
        )
        return f"{phase_summary}{gate_summary}"

    def run_memory_phases_and_gates(self, *, clusters_recorded: int, dry_run: bool) -> "tuple[str, bool, str]":
        """Run phases 4-6 then the §4 acceptance gates, gating success on the gates (#2545).

        Snapshots every discovered memory dir BEFORE the phases mutate it, runs the
        phases (capturing what decay archived per dir), snapshots AFTER, then runs the
        acceptance gates per dir (also populating the ``DreamQaProbe`` corpus).
        Returns ``(phase_summary, all_gates_passed, gate_summary)`` — a failing gate
        makes the caller stamp the pass attempted-not-succeeded rather than laundering
        a lossy consolidation into a success. A gate-evaluation failure for one dir is
        reported in the summary and FAILS CLOSED (that dir's verdict is FAIL, not PASS):
        the gates exist to catch a lossy consolidation, so a transient read error that
        disables the gate must never be laundered into an accepted pass. It still never
        crashes — the failure degrades to a WARN clause and a failed verdict.
        """
        from teatree.loops.dream import gates  # noqa: PLC0415 — deferred: loaded at tick time, not import
        from teatree.memory_audit import discover_memory_dirs  # noqa: PLC0415 — deferred: loaded at tick time

        memory_dirs = discover_memory_dirs()
        if not memory_dirs:
            return "", True, ""

        before = {d: gates.snapshot_memory_dir(d) for d in memory_dirs}
        schema_before = self._ledger_schema_count()
        phase_summary, archived_by_dir, maintenance_performed = self._run_phases(memory_dirs, dry_run=dry_run)
        schema_after = self._ledger_schema_count()

        all_passed, gate_summary = self._grade_dirs(
            before,
            memory_dirs,
            archived_by_dir=archived_by_dir,
            schema_before=schema_before,
            schema_after=schema_after,
            clusters_recorded=clusters_recorded,
            maintenance_performed=maintenance_performed,
            dry_run=dry_run,
        )
        return phase_summary, all_passed, gate_summary

    @staticmethod
    # ast-grep-ignore: ac-django-no-complexity-suppressions
    def _grade_dirs(  # noqa: PLR0913 — kwargs-only §4 gate inputs, threaded verbatim into run_acceptance_pass.
        before: "dict[Path, MemorySnapshot]",
        memory_dirs: list[Path],
        *,
        archived_by_dir: "dict[Path, tuple[ArchivedMemory, ...]]",
        schema_before: int,
        schema_after: int,
        clusters_recorded: int,
        maintenance_performed: bool,
        dry_run: bool,
    ) -> tuple[bool, str]:
        """Run the §4 acceptance gates per dir and aggregate the verdict + summary clause.

        The ONE gate-grading loop shared by the gated full path and the quiet-night
        path (F6.2), so both grade a file mutation identically. A gate-evaluation
        failure for one dir is reported and FAILS CLOSED (that dir's verdict is FAIL,
        never a laundered pass): the gates exist to catch a lossy consolidation, so a
        transient read error that disables the gate must never be accepted. It still
        never crashes — the failure degrades to a WARN clause and a failed verdict.
        """
        from teatree.loops.dream import acceptance, gates  # noqa: PLC0415 — deferred: loaded at tick time, not import
        from teatree.loops.dream.decay import ARCHIVE_DIRNAME  # noqa: PLC0415 — deferred: loaded at tick time

        all_passed = True
        clauses: list[str] = []
        for d in memory_dirs:
            try:
                report = acceptance.run_acceptance_pass(
                    before[d],
                    gates.snapshot_memory_dir(d),
                    overlay="",
                    scope=str(d),
                    archived=archived_by_dir.get(d, ()),
                    schema_before=schema_before,
                    schema_after=schema_after,
                    clusters_recorded=clusters_recorded,
                    maintenance_performed=maintenance_performed,
                    persist=not dry_run,
                    archive_dir=d / ARCHIVE_DIRNAME,
                )
            except Exception as exc:  # noqa: BLE001 — a gate failure degrades to a WARN clause, never aborts the phase
                all_passed = False  # fail closed — a raised gate is a FAILED gate, never an accepted pass
                clauses.append(f"WARN gates raised (verdict FAIL): {type(exc).__name__}: {exc}")
                continue
            all_passed = all_passed and report.passed
            if not report.passed:
                failed = ", ".join(g.name for g in report.gate_results if not g.passed)
                clauses.append(f"gates FAILED ({failed})")
        gate_summary = f"; {'; '.join(clauses)}" if clauses else "; all acceptance gates passed"
        return all_passed, gate_summary

    def _run_phases(
        self, memory_dirs: list[Path], *, dry_run: bool
    ) -> "tuple[str, dict[Path, tuple[ArchivedMemory, ...]], bool]":
        """Run phases 4 (cross-link) + 4b (merge) + 5 (re-index) + 6 (decay), re-indexing again after decay.

        Returns the summary clause, decay's archives per dir, and a
        ``maintenance_performed`` flag — True when ANY file-side phase did real work.
        Phase 4b (merge) runs AFTER cross-link and BEFORE re-index so the re-index
        drops the absorbed file's pointer. The decay phase reads the freshly-rendered
        index to detect budget pressure (#2723 budget tier), then a FINAL re-index
        drops the archived files' pointers so ``MEMORY.md`` falls back under the gate-(d)
        budget in the SAME pass — the AFTER snapshot the gates grade is accurate.
        """
        cross_link_summary, links_added = self._phase_summary(
            "cross-link", self._cross_link_one, memory_dirs, dry_run=dry_run
        )
        merge_summary, merged = self._merge_dirs(memory_dirs, dry_run=dry_run)
        reindex_summary, reindexed = self._phase_summary("re-index", self._reindex_one, memory_dirs, dry_run=dry_run)
        decay_summary, archived_by_dir = self._decay_dirs_with_archives(memory_dirs, dry_run=dry_run)
        archived = sum(len(a) for a in archived_by_dir.values())
        post_reindexed = 0
        if archived and not dry_run:
            _post_summary, post_reindexed = self._phase_summary(
                "re-index", self._reindex_one, memory_dirs, dry_run=dry_run
            )
        maintenance_performed = links_added > 0 or merged > 0 or reindexed > 0 or archived > 0 or post_reindexed > 0
        parts = [cross_link_summary, merge_summary, reindex_summary, decay_summary]
        return "".join(part for part in parts if part), archived_by_dir, maintenance_performed

    @staticmethod
    def _ledger_schema_count() -> int:
        """Total ConsolidatedMemory rows — the schema/cluster count for gate (c)."""
        model = cast("type[ConsolidatedMemory]", apps.get_model("core", "ConsolidatedMemory"))
        return model.objects.count()

    @staticmethod
    def _decay_dirs_with_archives(
        memory_dirs: list[Path], *, dry_run: bool
    ) -> "tuple[str, dict[Path, tuple[ArchivedMemory, ...]]]":
        """Run phase-6 decay per dir under fault isolation, returning its summary + archives.

        The budget tier is enabled (#2723): when ``MEMORY.md`` is over the load budget,
        decay ALSO archives the LOWEST-signal files the (empty) ledger home-rail can never
        reach — the reachable on-disk RETIRE for the curated corpus, just enough to bring
        the hot index back under budget while their signatures persist in MEMORY_ARCHIVE.md.
        """
        from teatree.loops.dream import decay  # noqa: PLC0415 — deferred: loaded at tick time, not import
        from teatree.loops.dream.loop import decay_enabled  # noqa: PLC0415 — deferred: loaded at tick time, not import

        archived_by_dir: dict[Path, tuple[ArchivedMemory, ...]] = {}
        if not decay_enabled():
            return "", archived_by_dir
        total = 0
        warnings: list[str] = []
        budget_policy = decay.DecayPolicy(budget_tier=decay.BudgetTier())
        for d in memory_dirs:
            try:
                result = decay.decay_memories(d, dry_run=dry_run, policy=budget_policy)
            except Exception as exc:  # noqa: BLE001 — one dir's decay failure degrades to a WARN, others proceed
                warnings.append(f"; WARN decay raised for {d.name}: {type(exc).__name__}: {exc}")
                continue
            archived_by_dir[d] = result.archived
            total += result.archived_count
        clause = f"; archived {total} stale memory(ies)" if total else ""
        return clause + "".join(warnings), archived_by_dir

    def _phase_summary(
        self, label: str, runner_one: _PhaseRunnerOne, memory_dirs: list[Path], *, dry_run: bool
    ) -> tuple[str, int]:
        """Run one phase over every dir, fault-isolated PER DIR.

        Returns ``(summary_clause, work_count)`` — the work count feeds the
        ``maintenance_performed`` gate signal. One dir raising degrades to a WARN
        clause naming that dir and the remaining dirs still run, so dir 1 failing
        never discards dirs 2..n.
        """
        count = 0
        warnings: list[str] = []
        for d in memory_dirs:
            try:
                count += runner_one(d, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 — one dir's failure degrades to a WARN, others proceed
                warnings.append(f"; WARN {label} raised for {d.name}: {type(exc).__name__}: {exc}")
        clause = self._phase_clause(label, count)
        parts = ([f"; {clause}"] if clause else []) + warnings
        return "".join(parts), count

    @staticmethod
    def _phase_clause(label: str, count: int) -> str:
        if not count:
            return ""
        return {
            "cross-link": f"cross-linked {count} memory edge(s)",
            "re-index": f"re-indexed {count} MEMORY.md",
            "decay": f"archived {count} stale memory(ies)",
        }[label]

    @staticmethod
    def _cross_link_one(d: Path, *, dry_run: bool) -> int:
        from teatree.loops.dream import cross_link  # noqa: PLC0415 — deferred: loaded at tick time, not import
        from teatree.loops.dream.loop import cross_link_enabled  # noqa: PLC0415 — deferred: loaded at tick time

        if not cross_link_enabled():
            return 0
        return cross_link.cross_link_memories(d, dry_run=dry_run).links_added

    def _merge_dirs(self, memory_dirs: list[Path], *, dry_run: bool) -> tuple[str, int]:
        """Phase 4b — merge near-duplicate memories per dir, fault-isolated (#2723).

        Collapses near-duplicate pairs (the higher-weight survivor keeps binding
        doctrine) and files a deduped reconciliation ticket for any two-BINDING
        conflict the merge phase refused to collapse (Decision-3). Returns the
        ``(summary_clause, merged_count)`` pair so the count feeds the
        ``maintenance_performed`` gate signal.
        """
        from teatree.loops.dream import merge  # noqa: PLC0415 — deferred: loaded at tick time, not import
        from teatree.loops.dream.loop import merge_enabled  # noqa: PLC0415 — deferred: loaded at tick time, not import

        if not merge_enabled():
            return "", 0
        merged = 0
        conflicts: list[merge.BindingConflict] = []
        warnings: list[str] = []
        for d in memory_dirs:
            try:
                result = merge.merge_memories(d, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 — one dir's merge failure degrades to a WARN, others proceed
                warnings.append(f"; WARN merge raised for {d.name}: {type(exc).__name__}: {exc}")
                continue
            merged += result.merged_count
            conflicts.extend(result.binding_conflicts)
        reconciled = self._file_binding_reconciliations(conflicts, dry_run=dry_run)
        clause = f"; merged {merged} near-duplicate memory(ies)" if merged else ""
        return f"{clause}{reconciled}{''.join(warnings)}", merged

    def _file_binding_reconciliations(self, conflicts: "list[BindingConflict]", *, dry_run: bool) -> str:
        """File a deduped reconciliation ticket per two-BINDING conflict, fault-isolated."""
        if not conflicts or dry_run:
            return ""
        try:
            from teatree.loops.dream import promote_memory  # noqa: PLC0415 — deferred: loaded at tick time, not import

            host, repo = self._backlog_host_resolver()
            if host is None:
                return "; WARN binding reconciliation skipped — no teatree code host resolved"
            outcomes = promote_memory.file_binding_reconciliation_tickets(
                host, repo=repo, conflicts=conflicts, dry_run=dry_run
            )
        except Exception as exc:  # noqa: BLE001 — a binding-reconciliation failure degrades to a WARN clause
            return f"; WARN binding reconciliation raised: {type(exc).__name__}: {exc}"
        filed = sum(1 for o in outcomes if o.filed)
        return f"; filed {filed} binding-reconciliation ticket(s)" if filed else ""

    @staticmethod
    def _reindex_one(d: Path, *, dry_run: bool) -> int:
        from teatree.loops.dream import reindex  # noqa: PLC0415 — deferred: loaded at tick time, not import
        from teatree.loops.dream.loop import reindex_enabled  # noqa: PLC0415 — deferred: loaded at tick time

        if not reindex_enabled():
            return 0
        return 1 if reindex.reindex_memory(d, dry_run=dry_run).changed else 0


__all__ = ["BacklogHostResolver", "MemoryPhaseRunner"]
