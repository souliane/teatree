"""``manage.py dream`` — drive the idle-time memory-consolidation cron (#1933).

The command owns the cron mechanics around the distillation engine
(:func:`teatree.loops.dream.engine.run_consolidation`, phases 1-3) and drives the
file-side phases 4-6 (cross-link / re-index / decay) after it:

``run`` is the manual escape hatch: it runs a pass NOW regardless of cadence,
with an optional ``--since`` window bound and a ``--dry-run`` no-write mode.
``tick`` is the cron entry point: it runs a pass only when the ``dream``
cadence has elapsed (``MiniLoopMarker``), bumping the cadence ledger on a fire.

Both acquire the in-flight ``LoopLease`` (``dream-tick``) first so two passes
never overlap — the loser SKIPs (the #786 WS2 CAS, correct on the prod SQLite
backend). On a successful pass the ``DreamRunMarker`` is stamped succeeded
(clearing the staleness alarm); a failed pass bumps only the attempt timestamp,
so staleness keeps firing until a clean run lands.

Anything touching the ORM is a management command (AGENTS.md § "Deciding Where
a New Command Lives"); ``t3 dream`` is the thin Typer wrapper that delegates
here via ``call_command``.
"""

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, cast

import typer
from django.apps import apps
from django_typer.management import TyperCommand, command

from teatree.core.backend_registry import get_backend_provider
from teatree.core.overlay_loader import get_all_overlays

if TYPE_CHECKING:
    from collections.abc import Callable

    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.core.models import ConsolidatedMemory
    from teatree.loops.dream.decay import ArchivedMemory


@dataclass(frozen=True, slots=True)
class PipelineMode:
    """The full-pipeline mode toggles for one dream pass.

    ``force_all_phases`` runs the WHOLE pipeline (core-gap tickets + LLM-derived
    eval staging); ``validate_live`` gates eval-promotion on a METERED live-model
    pass@k. Both are ``--full``-implied opt-ins, so they travel as ONE cohesive
    value rather than two loose flags threaded through every pass helper.
    """

    force_all_phases: bool = False
    validate_live: bool = False


_DEFAULT_MODE = PipelineMode()


class Command(TyperCommand):
    help = "Drive the idle-time memory-consolidation (dreaming) cron (#1933)."

    @command(name="run")
    def run(
        self,
        *,
        since: Annotated[
            str,
            typer.Option("--since", help="ISO-8601 lower bound for the replay window (default: engine lookback)."),
        ] = "",
        dry_run: Annotated[
            bool,
            typer.Option("--dry-run", help="Do everything except writing ConsolidatedMemory rows / the marker."),
        ] = False,
        propose_evals: Annotated[
            bool,
            typer.Option(
                "--propose-evals",
                help="Also derive inert eval candidates from grounded drift clusters (default OFF).",
            ),
        ] = False,
        full: Annotated[
            bool,
            typer.Option(
                "--full",
                help="Run the WHOLE pipeline: also file core-gap tickets and stage LLM-derived evals.",
            ),
        ] = False,
        validate_live: Annotated[
            bool,
            typer.Option(
                "--validate-live",
                help=(
                    "Gate eval-promotion on a METERED live-model pass@k (implied by --full). "
                    "Without it, candidates that clear the anti-vacuity guard are WITHHELD "
                    "rather than auto-landed in the gating suite."
                ),
            ),
        ] = False,
    ) -> None:
        """Run one consolidation pass NOW (manual escape hatch; ignores cadence)."""
        self._run_pass(
            since=_parse_since(since),
            dry_run=dry_run,
            enforce_cadence=False,
            propose_evals=propose_evals or full,
            mode=PipelineMode(force_all_phases=full, validate_live=validate_live or full),
        )

    @command(name="tick")
    def tick(self) -> None:
        """Run one consolidation pass IF the dream cadence has elapsed (cron entry).

        The eval-derivation seam is LIVE by default here (#2346): proposals are
        requested unless the ``T3_DREAM_PROPOSE_EVALS`` env / ``[loops.dream]
        propose_evals`` toml kill-switch disables it (see
        :func:`teatree.loops.dream.loop.propose_evals_enabled`).
        """
        from teatree.loops.dream.loop import propose_evals_enabled  # noqa: PLC0415

        self._run_pass(since=None, dry_run=False, enforce_cadence=True, propose_evals=propose_evals_enabled())

    @command(name="compliance")
    def compliance(self) -> None:
        """Print the latest instruction-compliance snapshot — read-only (#2663)."""
        from teatree.loops.dream.compliance import render_compliance_show  # noqa: PLC0415

        for line in render_compliance_show():
            self.stdout.write(line)

    def _run_pass(
        self,
        *,
        since: dt.datetime | None,
        dry_run: bool,
        enforce_cadence: bool,
        propose_evals: bool,
        mode: PipelineMode = _DEFAULT_MODE,
    ) -> None:
        import os  # noqa: PLC0415

        from django.utils import timezone  # noqa: PLC0415

        from teatree.core.models import DreamRunMarker, LoopLease, MiniLoopMarker  # noqa: PLC0415
        from teatree.loops.config import LoopsConfig  # noqa: PLC0415
        from teatree.loops.dream.loop import DREAM_LEASE_NAME, DREAM_LEASE_SECONDS, MINI_LOOP  # noqa: PLC0415
        from teatree.loops.gating import elapsed_and_enabled  # noqa: PLC0415

        now = timezone.now()
        if enforce_cadence:
            decision = elapsed_and_enabled(LoopsConfig.load(), MINI_LOOP, now)
            if not decision.should_fire:
                self.stdout.write(f"SKIP  dream cadence not elapsed ({decision.skip_reason}).")
                return

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire(DREAM_LEASE_NAME, owner=owner, lease_seconds=DREAM_LEASE_SECONDS):
            self.stdout.write("SKIP  another dream pass is already running — dream-tick lease held.")
            return

        enabled = propose_evals or _env_propose_evals()
        try:
            succeeded = self._consolidate_and_mark(
                since=since, dry_run=dry_run, now=now, propose_evals=enabled, mode=mode
            )
        finally:
            LoopLease.objects.release(DREAM_LEASE_NAME, owner=owner)

        if enforce_cadence and succeeded:
            MiniLoopMarker.objects.mark_fired(MINI_LOOP.name, now)

        # Re-read confirmation so a stamped success can be cited (resilience #7).
        if not dry_run:
            marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
            stamped = marker.last_succeeded_at.isoformat() if marker and marker.last_succeeded_at else "none"
            self.stdout.write(f"      dream marker last_succeeded_at={stamped}")

    def _consolidate_and_mark(
        self,
        *,
        since: dt.datetime | None,
        dry_run: bool,
        now: dt.datetime,
        propose_evals: bool,
        mode: PipelineMode = _DEFAULT_MODE,
    ) -> bool:
        from teatree.core.models import DreamRunMarker  # noqa: PLC0415
        from teatree.loops.dream import engine  # noqa: PLC0415
        from teatree.loops.dream.eval_proposer import EvalProposalRequest  # noqa: PLC0415

        request = EvalProposalRequest() if propose_evals else None
        try:
            result = engine.run_consolidation(overlay="", since=since, dry_run=dry_run, eval_proposals=request)
        except Exception as exc:  # noqa: BLE001
            if not dry_run:
                DreamRunMarker.objects.mark_attempted(now)
            self.stdout.write(f"FAIL  dream pass raised: {type(exc).__name__}: {exc}")
            return False

        evals = f"; {result.evals_proposed} eval candidate(s)" if result.evals_proposed else ""
        empty = (
            f"; WARN {result.empty_batches} batch(es) returned 0 clusters from non-empty input"
            if result.empty_batches
            else ""
        )
        if dry_run:
            self.stdout.write(
                f"DRY   dream pass — {result.clusters_recorded} cluster(s) would be recorded "
                f"from {result.members_replayed} member(s){evals}{empty}; no rows or marker written.",
            )
            return False

        if result.members_replayed == 0:
            # No transcript was replayed, so nothing was distilled — the
            # consolidation pass stays attempted-not-succeeded (staleness keeps
            # firing). But the file-side phases 4-6 operate on the on-disk memory
            # set (discover_memory_dirs), independent of the transcript extract,
            # so a 0-member pass must still run them — otherwise decay can never
            # archive stale memories and the index is never re-derived on a quiet
            # night (#2547).
            memory_phases = self._run_memory_phases(dry_run=dry_run)
            DreamRunMarker.objects.mark_attempted(now)
            self.stdout.write(
                f"WARN  dream pass found 0 transcript members — marker NOT stamped succeeded{memory_phases}.",
            )
            return False

        promoted = self._promote_candidates(
            propose_evals=propose_evals,
            dry_run=dry_run,
            force_all_phases=mode.force_all_phases,
            validate_live=mode.validate_live,
        )
        # Phase 3c (#2663) runs BEFORE the gates so gate (g) reads the just-persisted
        # compliance records (a recurrence remediated with a memory FAILS the pass).
        compliance = self._run_compliance_phase(since=since, dry_run=dry_run, force_all_phases=mode.force_all_phases)
        memory_phases, gates_passed, gates_summary = self._run_memory_phases_and_gates(
            clusters_recorded=result.clusters_recorded, dry_run=dry_run
        )
        memory_promote = self._run_memory_promotion(dry_run=dry_run, force_all_phases=mode.force_all_phases)

        # The §4 acceptance gates make the pass anti-vacuous: a lossy / delete-only
        # / no-op consolidation FAILS a gate, and a failing gate must NOT stamp
        # success — staleness keeps firing until a faithful pass lands (#2545).
        if not gates_passed:
            DreamRunMarker.objects.mark_attempted(now)
            self.stdout.write(
                f"WARN  dream pass — {result.clusters_recorded} cluster(s) recorded "
                f"from {result.members_replayed} member(s){evals}{empty}{promoted}{compliance}{memory_phases}"
                f"{memory_promote}{gates_summary}; acceptance gate(s) FAILED — marker NOT stamped succeeded.",
            )
            return False

        DreamRunMarker.objects.mark_succeeded(now)
        self.stdout.write(
            f"OK    dream pass — {result.clusters_recorded} cluster(s) recorded "
            f"from {result.members_replayed} member(s){evals}{empty}{promoted}{compliance}{memory_phases}"
            f"{memory_promote}{gates_summary}.",
        )
        return True

    def _run_compliance_phase(self, *, since: "dt.datetime | None", dry_run: bool, force_all_phases: bool) -> str:
        """Phase 3c — the instruction-compliance accountant (#2663; never raises).

        Runs only when the default-OFF ``compliance`` toggle is on (env / toml) AND
        the ``--full`` pipeline is requested — it FILES enforcement tickets, mirroring
        the Pass-2 memory-promotion posture. The detect → persist → escalate work
        lives in :func:`teatree.loops.dream.compliance.run_compliance_phase`; this
        wires the gating + the resolved backlog host and fault-isolates the phase.
        """
        from teatree.loops.dream.loop import compliance_enabled  # noqa: PLC0415

        if not force_all_phases or not compliance_enabled():
            return ""
        try:
            from teatree.loops.dream import compliance  # noqa: PLC0415

            host, repo = self._teatree_backlog_host()
            return compliance.run_compliance_phase(since=since, dry_run=dry_run, host=host, repo=repo)
        except Exception as exc:  # noqa: BLE001
            return f"; WARN compliance phase raised: {type(exc).__name__}: {exc}"

    def _promote_candidates(
        self, *, propose_evals: bool, dry_run: bool, force_all_phases: bool = False, validate_live: bool = False
    ) -> str:
        """Promote the freshly-derived candidates to live scenarios (guarded; never raises).

        Runs only when proposals were requested. Each candidate clears the
        NON-BYPASSABLE anti-vacuity guard
        (:func:`teatree.loops.dream.promote.guard_can_fail`) AND a live-model pass@k
        before it lands. *validate_live* (set by ``--validate-live`` / ``--full``)
        supplies the real METERED validator (:func:`promote.build_live_validator`);
        WITHOUT it nothing auto-lands — every clearing candidate is WITHHELD, the key
        safety property for the nightly ``tick``. A promotion failure is reported in
        the summary line, never crashing the pass that already stamped success. When
        the default-OFF LLM derivation (#2447) is enabled, each candidate is
        additionally synthesized into a full scenario and STAGED (never auto-committed).
        """
        if not propose_evals:
            return ""
        try:
            from teatree.loops.dream import promote  # noqa: PLC0415
            from teatree.loops.dream.eval_proposer import _default_proposals_path  # noqa: PLC0415

            validator = promote.build_live_validator() if validate_live else None
            outcomes = promote.promote_proposals_file(
                _default_proposals_path(), dry_run=dry_run, live_gate=promote.LiveGate(validator=validator)
            )
        except Exception as exc:  # noqa: BLE001
            return f"; WARN eval promotion raised: {type(exc).__name__}: {exc}"
        promoted = sum(1 for o in outcomes if o.promoted)
        withheld = len(outcomes) - promoted
        derived = self._derive_evals(dry_run=dry_run, force_all_phases=force_all_phases)
        if not outcomes:
            return derived
        return f"; promoted {promoted} live eval(s), withheld {withheld} unvalidated candidate(s){derived}"

    def _derive_evals(self, *, dry_run: bool, force_all_phases: bool = False) -> str:
        """Stage LLM-derived full scenarios from the candidate queue (default OFF; never raises).

        Runs only when the default-OFF ``derive_evals`` toggle is on (#2447). Each
        candidate is synthesized into a full ``under_load`` scenario, teeth-checked,
        and STAGED for a human/maker to ratify via a PR — never auto-committed to the
        live suite. A failure is reported in the summary line, never crashing the pass.
        """
        from teatree.loops.dream.loop import derive_evals_enabled  # noqa: PLC0415

        if not force_all_phases and not derive_evals_enabled():
            return ""
        try:
            from teatree.loops.dream import llm_eval_proposer  # noqa: PLC0415
            from teatree.loops.dream.eval_proposer import _default_proposals_path  # noqa: PLC0415

            outcomes = llm_eval_proposer.stage_proposals_file(_default_proposals_path(), dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            return f"; WARN eval derivation raised: {type(exc).__name__}: {exc}"
        if not outcomes:
            return ""
        staged = sum(1 for o in outcomes if o.derived)
        return f"; staged {staged} derived eval(s) for review, dropped {len(outcomes) - staged}"

    def _run_memory_promotion(self, *, dry_run: bool, force_all_phases: bool = False) -> str:
        """Pass 2 — triage the ledger, ticket each core-gap, retire resolved memories (#2426).

        Runs only when the default-OFF ``memory_promote`` toggle is on, because it
        FILES backlog tickets. Resolves the teatree backlog code host, triages every
        untriaged ``ConsolidatedMemory`` row, files a deduped ``needs-triage`` ticket
        for each core-generic gap, and retires any TICKETED memory whose linked ticket
        has closed. A failure is reported in the summary line, never crashing the pass.
        """
        from teatree.loops.dream.loop import memory_promote_enabled  # noqa: PLC0415

        if not force_all_phases and not memory_promote_enabled():
            return ""
        try:
            from teatree.loops.dream import promote_memory  # noqa: PLC0415

            host, repo = self._teatree_backlog_host()
            if host is None:
                return "; WARN memory promotion skipped — no teatree code host resolved"
            filed = promote_memory.file_core_gap_tickets(host, repo=repo, dry_run=dry_run)
            retired = [] if dry_run else promote_memory.retire_resolved_memories(host)
        except Exception as exc:  # noqa: BLE001
            return f"; WARN memory promotion raised: {type(exc).__name__}: {exc}"
        new_tickets = sum(1 for o in filed if o.filed)
        if not filed and not retired:
            return ""
        return f"; ticketed {new_tickets} core-gap memory(ies), retired {len(retired)}"

    @staticmethod
    def _teatree_backlog_host() -> "tuple[CodeHostBackend | None, str]":
        """Resolve the teatree backlog code host + repo slug for Pass-2 ticket filing."""
        repo = "souliane/teatree"
        provider = get_backend_provider()
        for overlay in get_all_overlays().values():
            host = provider.get_code_host(overlay)
            if host is not None:
                return host, repo
        return None, repo

    def _run_memory_phases(self, *, dry_run: bool) -> str:
        """Run the memory-file phases 4-6 over every discovered memory dir, fault-isolated.

        Phases 4 (cross-link), 5 (re-index), and 6 (decay/archive) each run LIVE by
        default behind their own ``[loops.dream]`` / ``T3_DREAM_*`` kill-switch and
        each in its own try/except, so one phase (or one memory dir) failing never
        crashes the tick or stops the other phases. Operates only on the discovered
        ``~/.claude`` memory dirs (engine reuse); the units under test inject a tmp
        dir directly into the phase functions, never the real one.
        """
        from teatree.memory_audit import discover_memory_dirs  # noqa: PLC0415

        memory_dirs = discover_memory_dirs()
        if not memory_dirs:
            return ""
        phases: list[tuple[str, Callable[..., int]]] = [
            ("cross-link", self._cross_link_dirs),
            ("re-index", self._reindex_dirs),
            ("decay", self._decay_dirs),
        ]
        parts = [self._phase_summary(label, runner, memory_dirs, dry_run=dry_run)[0] for label, runner in phases]
        return "".join(part for part in parts if part)

    def _run_memory_phases_and_gates(self, *, clusters_recorded: int, dry_run: bool) -> "tuple[str, bool, str]":
        """Run phases 4-6 then the §4 acceptance gates, gating success on the gates (#2545).

        Snapshots every discovered memory dir BEFORE the phases mutate it, runs the
        phases (capturing what decay archived per dir), snapshots AFTER, then runs the
        six acceptance gates per dir (:func:`teatree.loops.dream.gates.run_acceptance_pass`),
        which also populates the ``DreamQaProbe`` corpus. Returns
        ``(phase_summary, all_gates_passed, gate_summary)`` — a failing gate makes the
        caller stamp the pass attempted-not-succeeded (staleness keeps firing) rather
        than laundering a lossy consolidation into a success.

        Fault-isolated: a gate-evaluation failure for one dir is reported in the
        summary, defaults that dir's verdict to PASS (the gate machinery, not the
        consolidation, broke), and never crashes the tick.
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
        """Run phases 4 (cross-link) + 5 (re-index) then decay.

        Returns the summary clause, decay's archives per dir, and a
        ``maintenance_performed`` flag — True when ANY file-side phase did real
        work (cross-link edges added, MEMORY.md re-indexed, or stale memories
        archived). The gate uses that flag to count a quiet 0-cluster maintenance
        pass as genuine consolidation (#2626).
        """
        cross_link_summary, links_added = self._phase_summary(
            "cross-link", self._cross_link_dirs, memory_dirs, dry_run=dry_run
        )
        reindex_summary, reindexed = self._phase_summary("re-index", self._reindex_dirs, memory_dirs, dry_run=dry_run)
        decay_summary, archived_by_dir = self._decay_dirs_with_archives(memory_dirs, dry_run=dry_run)
        archived = sum(len(a) for a in archived_by_dir.values())
        maintenance_performed = links_added > 0 or reindexed > 0 or archived > 0
        parts = [cross_link_summary, reindex_summary, decay_summary]
        return "".join(part for part in parts if part), archived_by_dir, maintenance_performed

    @staticmethod
    def _ledger_schema_count() -> int:
        """Total ConsolidatedMemory rows — the schema/cluster count for gate (c)."""
        consolidated_memory_model = cast("type[ConsolidatedMemory]", apps.get_model("core", "ConsolidatedMemory"))
        return consolidated_memory_model.objects.count()

    def _decay_dirs_with_archives(
        self, memory_dirs: list[Path], *, dry_run: bool
    ) -> "tuple[str, dict[Path, tuple[ArchivedMemory, ...]]]":
        """Run phase-6 decay per dir under fault isolation, returning its summary + archives."""
        from teatree.loops.dream import decay  # noqa: PLC0415
        from teatree.loops.dream.loop import decay_enabled  # noqa: PLC0415

        archived_by_dir: dict[Path, tuple[ArchivedMemory, ...]] = {}
        if not decay_enabled():
            return "", archived_by_dir
        total = 0
        for d in memory_dirs:
            try:
                result = decay.decay_memories(d, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001
                return f"; WARN decay raised: {type(exc).__name__}: {exc}", archived_by_dir
            archived_by_dir[d] = result.archived
            total += result.archived_count
        return (f"; archived {total} stale memory(ies)" if total else ""), archived_by_dir

    def _phase_summary(
        self,
        label: str,
        runner: "Callable[..., int]",
        memory_dirs: list[Path],
        *,
        dry_run: bool,
    ) -> tuple[str, int]:
        """Run one phase's per-dir runner under fault isolation.

        Returns ``(summary_clause, work_count)`` — the work count is the phase's
        own unit (edges added / MEMORY.md re-indexed / memories archived) and feeds
        the ``maintenance_performed`` gate signal. A fault-isolated failure returns
        a WARN clause and a 0 count (the phase did no provable work).
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

    def _cross_link_dirs(self, memory_dirs: list[Path], *, dry_run: bool) -> int:
        from teatree.loops.dream import cross_link  # noqa: PLC0415
        from teatree.loops.dream.loop import cross_link_enabled  # noqa: PLC0415

        if not cross_link_enabled():
            return 0
        return sum(cross_link.cross_link_memories(d, dry_run=dry_run).links_added for d in memory_dirs)

    def _reindex_dirs(self, memory_dirs: list[Path], *, dry_run: bool) -> int:
        from teatree.loops.dream import reindex  # noqa: PLC0415
        from teatree.loops.dream.loop import reindex_enabled  # noqa: PLC0415

        if not reindex_enabled():
            return 0
        return sum(1 for d in memory_dirs if reindex.reindex_memory(d, dry_run=dry_run).changed)

    def _decay_dirs(self, memory_dirs: list[Path], *, dry_run: bool) -> int:
        from teatree.loops.dream import decay  # noqa: PLC0415
        from teatree.loops.dream.loop import decay_enabled  # noqa: PLC0415

        if not decay_enabled():
            return 0
        return sum(decay.decay_memories(d, dry_run=dry_run).archived_count for d in memory_dirs)


def _env_propose_evals() -> bool:
    """Read the ``T3_DREAM_PROPOSE_EVALS`` opt-in env for the manual ``run`` path.

    The manual ``run`` enables the eval phase when ``--propose-evals`` is given OR
    this env is truthy (``1``/``true``/``yes``, case-insensitive). The cadence-
    driven ``tick`` path does NOT route through here — it resolves the seam (LIVE
    by default, env/toml kill-switch) via
    :func:`teatree.loops.dream.loop.propose_evals_enabled`.
    """
    import os  # noqa: PLC0415

    return os.environ.get("T3_DREAM_PROPOSE_EVALS", "").strip().lower() in {"1", "true", "yes"}


def _parse_since(raw: str) -> dt.datetime | None:
    """Parse the ``--since`` ISO-8601 string; empty → ``None`` (engine default).

    A naive value (``--since 2026-06-01``) is normalized to the current
    timezone so the ``USE_TZ`` engine never compares naive against aware. A
    malformed value raises ``CommandError`` instead of a raw traceback.
    """
    from django.core.management.base import CommandError  # noqa: PLC0415
    from django.utils import timezone  # noqa: PLC0415

    value = raw.strip()
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        msg = f"--since is not a valid ISO-8601 datetime: {value!r}"
        raise CommandError(msg) from exc
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed)
    return parsed
