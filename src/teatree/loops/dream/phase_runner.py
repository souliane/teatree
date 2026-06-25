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
from typing import TYPE_CHECKING

from django.apps import apps

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.core.models import ConsolidatedMemory
    from teatree.loops.dream.decay import ArchivedMemory
    from teatree.loops.dream.merge import BindingConflict

#: Resolve the teatree backlog code host + repo slug for ticket filing.
BacklogHostResolver = Callable[[], "tuple[CodeHostBackend | None, str]"]


class MemoryPhaseRunner:
    """Runs the file-side dream phases (4 / 4b / 5 / 6) and the §4 acceptance gates.

    Composed by the ``dream`` command; the binding-reconciliation filer reaches the
    command's backlog host through the injected *backlog_host_resolver*.
    """

    def __init__(self, *, backlog_host_resolver: BacklogHostResolver) -> None:
        self._backlog_host_resolver = backlog_host_resolver

    def run_memory_phases(self, *, dry_run: bool) -> str:
        """Run phases 4-6 over every discovered memory dir, fault-isolated.

        The quiet-night path (0 transcript members): no gates, just the file-side
        maintenance over the discovered ``~/.claude`` memory dirs.
        """
        from teatree.memory_audit import discover_memory_dirs  # noqa: PLC0415

        memory_dirs = discover_memory_dirs()
        if not memory_dirs:
            return ""
        phases: list[tuple[str, Callable[..., int]]] = [
            ("re-index", self._reindex_dirs),
            ("decay", self._decay_dirs),
        ]
        cross_link_part = self._phase_summary("cross-link", self._cross_link_dirs, memory_dirs, dry_run=dry_run)[0]
        merge_part = self._merge_dirs(memory_dirs, dry_run=dry_run)[0]
        rest = [self._phase_summary(label, runner, memory_dirs, dry_run=dry_run)[0] for label, runner in phases]
        parts = [cross_link_part, merge_part, *rest]
        return "".join(part for part in parts if part)

    def run_memory_phases_and_gates(self, *, clusters_recorded: int, dry_run: bool) -> "tuple[str, bool, str]":
        """Run phases 4-6 then the §4 acceptance gates, gating success on the gates (#2545).

        Snapshots every discovered memory dir BEFORE the phases mutate it, runs the
        phases (capturing what decay archived per dir), snapshots AFTER, then runs the
        acceptance gates per dir (also populating the ``DreamQaProbe`` corpus).
        Returns ``(phase_summary, all_gates_passed, gate_summary)`` — a failing gate
        makes the caller stamp the pass attempted-not-succeeded rather than laundering
        a lossy consolidation into a success. A gate-evaluation failure for one dir is
        reported in the summary, defaults that dir's verdict to PASS, and never crashes.
        """
        from teatree.loops.dream import gates  # noqa: PLC0415
        from teatree.memory_audit import discover_memory_dirs  # noqa: PLC0415

        memory_dirs = discover_memory_dirs()
        if not memory_dirs:
            return "", True, ""

        before = {d: gates.snapshot_memory_dir(d) for d in memory_dirs}
        schema_before = self._ledger_schema_count()
        phase_summary, archived_by_dir, maintenance_performed = self._run_phases(memory_dirs, dry_run=dry_run)
        schema_after = self._ledger_schema_count()

        all_passed = True
        clauses: list[str] = []
        for d in memory_dirs:
            try:
                report = gates.run_acceptance_pass(
                    before[d],
                    gates.snapshot_memory_dir(d),
                    overlay="",
                    archived=archived_by_dir.get(d, ()),
                    schema_before=schema_before,
                    schema_after=schema_after,
                    clusters_recorded=clusters_recorded,
                    maintenance_performed=maintenance_performed,
                    persist=not dry_run,
                )
            except Exception as exc:  # noqa: BLE001
                clauses.append(f"WARN gates raised: {type(exc).__name__}: {exc}")
                continue
            all_passed = all_passed and report.passed
            if not report.passed:
                failed = ", ".join(g.name for g in report.gate_results if not g.passed)
                clauses.append(f"gates FAILED ({failed})")
        gate_summary = f"; {'; '.join(clauses)}" if clauses else "; all acceptance gates passed"
        return phase_summary, all_passed, gate_summary

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
            "cross-link", self._cross_link_dirs, memory_dirs, dry_run=dry_run
        )
        merge_summary, merged = self._merge_dirs(memory_dirs, dry_run=dry_run)
        reindex_summary, reindexed = self._phase_summary("re-index", self._reindex_dirs, memory_dirs, dry_run=dry_run)
        decay_summary, archived_by_dir = self._decay_dirs_with_archives(memory_dirs, dry_run=dry_run)
        archived = sum(len(a) for a in archived_by_dir.values())
        post_reindexed = 0
        if archived and not dry_run:
            _post_summary, post_reindexed = self._phase_summary(
                "re-index", self._reindex_dirs, memory_dirs, dry_run=dry_run
            )
        maintenance_performed = links_added > 0 or merged > 0 or reindexed > 0 or archived > 0 or post_reindexed > 0
        parts = [cross_link_summary, merge_summary, reindex_summary, decay_summary]
        return "".join(part for part in parts if part), archived_by_dir, maintenance_performed

    @staticmethod
    def _ledger_schema_count() -> int:
        """Total ConsolidatedMemory rows — the schema/cluster count for gate (c)."""
        from typing import cast  # noqa: PLC0415

        model = cast("type[ConsolidatedMemory]", apps.get_model("core", "ConsolidatedMemory"))
        return model.objects.count()

    @staticmethod
    def _decay_dirs_with_archives(
        memory_dirs: list[Path], *, dry_run: bool
    ) -> "tuple[str, dict[Path, tuple[ArchivedMemory, ...]]]":
        """Run phase-6 decay per dir under fault isolation, returning its summary + archives.

        The budget tier is enabled (#2723): when ``MEMORY.md`` is over the load budget,
        decay ALSO archives old, unreferenced, duplicated files the (empty) ledger
        home-rail can never reach — the reachable on-disk RETIRE for the curated corpus.
        """
        from teatree.loops.dream import decay  # noqa: PLC0415
        from teatree.loops.dream.loop import decay_enabled  # noqa: PLC0415

        archived_by_dir: dict[Path, tuple[ArchivedMemory, ...]] = {}
        if not decay_enabled():
            return "", archived_by_dir
        total = 0
        budget_policy = decay.DecayPolicy(budget_tier=decay.BudgetTier())
        for d in memory_dirs:
            try:
                result = decay.decay_memories(d, dry_run=dry_run, policy=budget_policy)
            except Exception as exc:  # noqa: BLE001
                return f"; WARN decay raised: {type(exc).__name__}: {exc}", archived_by_dir
            archived_by_dir[d] = result.archived
            total += result.archived_count
        return (f"; archived {total} stale memory(ies)" if total else ""), archived_by_dir

    def _phase_summary(
        self, label: str, runner: "Callable[..., int]", memory_dirs: list[Path], *, dry_run: bool
    ) -> tuple[str, int]:
        """Run one phase's per-dir runner under fault isolation.

        Returns ``(summary_clause, work_count)`` — the work count feeds the
        ``maintenance_performed`` gate signal. A fault-isolated failure returns a
        WARN clause and a 0 count (the phase did no provable work).
        """
        try:
            count = runner(memory_dirs, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            return f"; WARN {label} raised: {type(exc).__name__}: {exc}", 0
        clause = self._phase_clause(label, count)
        return (f"; {clause}" if clause else ""), count

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
    def _cross_link_dirs(memory_dirs: list[Path], *, dry_run: bool) -> int:
        from teatree.loops.dream import cross_link  # noqa: PLC0415
        from teatree.loops.dream.loop import cross_link_enabled  # noqa: PLC0415

        if not cross_link_enabled():
            return 0
        return sum(cross_link.cross_link_memories(d, dry_run=dry_run).links_added for d in memory_dirs)

    def _merge_dirs(self, memory_dirs: list[Path], *, dry_run: bool) -> tuple[str, int]:
        """Phase 4b — merge near-duplicate memories per dir, fault-isolated (#2723).

        Collapses near-duplicate pairs (the higher-weight survivor keeps binding
        doctrine) and files a deduped reconciliation ticket for any two-BINDING
        conflict the merge phase refused to collapse (Decision-3). Returns the
        ``(summary_clause, merged_count)`` pair so the count feeds the
        ``maintenance_performed`` gate signal.
        """
        from teatree.loops.dream import merge  # noqa: PLC0415
        from teatree.loops.dream.loop import merge_enabled  # noqa: PLC0415

        if not merge_enabled():
            return "", 0
        try:
            merged = 0
            conflicts: list[merge.BindingConflict] = []
            for d in memory_dirs:
                result = merge.merge_memories(d, dry_run=dry_run)
                merged += result.merged_count
                conflicts.extend(result.binding_conflicts)
        except Exception as exc:  # noqa: BLE001
            return f"; WARN merge raised: {type(exc).__name__}: {exc}", 0
        reconciled = self._file_binding_reconciliations(conflicts, dry_run=dry_run)
        clause = f"; merged {merged} near-duplicate memory(ies)" if merged else ""
        return f"{clause}{reconciled}", merged

    def _file_binding_reconciliations(self, conflicts: "list[BindingConflict]", *, dry_run: bool) -> str:
        """File a deduped reconciliation ticket per two-BINDING conflict, fault-isolated."""
        if not conflicts or dry_run:
            return ""
        try:
            from teatree.loops.dream import promote_memory  # noqa: PLC0415

            host, repo = self._backlog_host_resolver()
            if host is None:
                return "; WARN binding reconciliation skipped — no teatree code host resolved"
            outcomes = promote_memory.file_binding_reconciliation_tickets(
                host, repo=repo, conflicts=conflicts, dry_run=dry_run
            )
        except Exception as exc:  # noqa: BLE001
            return f"; WARN binding reconciliation raised: {type(exc).__name__}: {exc}"
        filed = sum(1 for o in outcomes if o.filed)
        return f"; filed {filed} binding-reconciliation ticket(s)" if filed else ""

    @staticmethod
    def _reindex_dirs(memory_dirs: list[Path], *, dry_run: bool) -> int:
        from teatree.loops.dream import reindex  # noqa: PLC0415
        from teatree.loops.dream.loop import reindex_enabled  # noqa: PLC0415

        if not reindex_enabled():
            return 0
        return sum(1 for d in memory_dirs if reindex.reindex_memory(d, dry_run=dry_run).changed)

    @staticmethod
    def _decay_dirs(memory_dirs: list[Path], *, dry_run: bool) -> int:
        from teatree.loops.dream import decay  # noqa: PLC0415
        from teatree.loops.dream.loop import decay_enabled  # noqa: PLC0415

        if not decay_enabled():
            return 0
        return sum(decay.decay_memories(d, dry_run=dry_run).archived_count for d in memory_dirs)


__all__ = ["BacklogHostResolver", "MemoryPhaseRunner"]
