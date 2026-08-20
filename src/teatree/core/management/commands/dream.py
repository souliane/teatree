"""``manage.py dream`` — drive the idle-time memory-consolidation cron (#1933).

The command owns the cron mechanics around the distillation engine
(:func:`teatree.loops.dream.engine.run_consolidation`, phases 1-3) and drives the
file-side phases 4-6 (cross-link / re-index / decay) after it:

``run`` is the manual escape hatch: it runs a pass NOW regardless of cadence,
with an optional ``--since`` window bound and a ``--dry-run`` no-write mode.
``tick`` is the off-live-tick entry the worker's ``drive_off_live_tick_loops`` chain
fires: it runs a pass only when the ``dream`` Loop
row is due (``Loop.is_due`` / ``last_run_at`` — the ONE cadence ledger) and the
single enable verdict (``Loop.enabled`` + ``LoopState``) admits it, bumping
``last_run_at`` on a successful fire.

Both acquire the in-flight ``LoopLease`` (``dream-tick``) first so two passes
never overlap — the loser SKIPs (the #786 WS2 CAS, correct on the prod SQLite
backend), unless the holder's pid is PROVABLY dead, in which case
:mod:`teatree.loops.dream.lease` reclaims it. On a successful pass the
``DreamRunMarker`` is stamped succeeded (clearing the staleness alarm); a failed pass
bumps only the attempt timestamp, so staleness keeps firing until a clean run lands.

The process EXIT CODE mirrors that marker (:class:`PassOutcome`, #3993): a pass that
ran and did not stamp success exits non-zero, so a caller can tell a blocked pass from
a healthy one without reading a worker log.

Anything touching the ORM is a management command (AGENTS.md § "Deciding Where
a New Command Lives"); ``t3 dream`` is the thin Typer wrapper that delegates
here via ``call_command``.
"""

import datetime as dt
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.backend_registry import get_backend_provider
from teatree.core.management.commands._dream_report import _ResultFragments
from teatree.core.overlay_loader import get_all_overlays
from teatree.loops.dream.pass_config import PassBudget, PromotionBudget

if TYPE_CHECKING:
    from teatree.core.backend_protocols import CodeHostBackend
    from teatree.loops.dream.engine import DreamRunResult
    from teatree.loops.dream.gap_phases import GapPromotionPhases
    from teatree.loops.dream.phase_runner import MemoryPhaseRunner


@dataclass(frozen=True, slots=True)
class PipelineMode:
    """The pipeline toggles for one dream pass.

    ``force_all_phases`` runs the WHOLE pipeline (core-gap tickets + LLM-derived
    eval staging); ``validate_live`` gates eval-promotion on a METERED live-model
    pass@k. Both are ``--full``-implied opt-ins, so they travel as ONE cohesive
    value rather than loose flags threaded through every pass helper.
    ``propose_evals`` — whether the pass derives inert eval candidates at all — is
    resolved differently per entry point (a flag on ``run``, the default-ON env/DB
    kill-switch on ``tick``) but is the same kind of thing once resolved, so it rides
    here too rather than as a sixth loose parameter.

    ``force_all_phases`` is a CONVENIENCE alias for one manual pass, never a phase's
    only way in: ``tick`` cannot set it, so a phase gated on it AND its own toggle is
    dead on the cron path however that toggle is configured (#4176). Every phase gate is
    therefore its own toggle alone, or ``force_all_phases`` OR that toggle.
    """

    force_all_phases: bool = False
    validate_live: bool = False
    propose_evals: bool = False


_DEFAULT_MODE = PipelineMode()


class PassOutcome(StrEnum):
    """What one invocation did — the sole input to the process exit code (#3993).

    The exit code MIRRORS the marker: a pass that ran and did not stamp
    ``last_succeeded_at`` is ``FAILED`` and exits non-zero, so a blocked pass stops
    being indistinguishable from a healthy one to its caller. ``SKIPPED`` (disabled,
    not due, lease held) never ran a pass, and ``DRY_RUN`` never writes a marker by
    design — neither is a failure, so both exit 0.
    """

    STAMPED = "stamped"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"
    FAILED = "failed"


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
        _surface_exit_code(
            self._run_pass(
                since=_parse_since(since),
                dry_run=dry_run,
                enforce_cadence=False,
                mode=PipelineMode(
                    force_all_phases=full,
                    validate_live=validate_live or full,
                    propose_evals=propose_evals or full,
                ),
            )
        )

    @command(name="tick")
    def tick(self) -> None:
        """Run one consolidation pass IF the dream cadence has elapsed (cron entry).

        The eval-derivation seam is LIVE by default here (#2346): proposals are
        requested unless the ``T3_DREAM_PROPOSE_EVALS`` env / ``[loops.dream]
        propose_evals`` toml kill-switch disables it (see
        :func:`teatree.loops.dream.loop.propose_evals_enabled`).

        ``force_all_phases`` stays False — ``--full`` is the manual pass's alias — but
        ``validate_live`` is resolved from config so the METERED live-promotion gate has
        a cron-reachable path at all (default OFF, so the nightly pass still withholds).
        """
        from teatree.loops.dream.loop import propose_evals_enabled  # noqa: PLC0415 — deferred: lazy command import
        from teatree.loops.dream.pass_config import validate_live_enabled  # noqa: PLC0415 — deferred: lazy import

        _surface_exit_code(
            self._run_pass(
                since=None,
                dry_run=False,
                enforce_cadence=True,
                mode=PipelineMode(validate_live=validate_live_enabled(), propose_evals=propose_evals_enabled()),
            )
        )

    @command(name="compliance")
    def compliance(self) -> None:
        """Print the latest instruction-compliance snapshot — read-only (#2663)."""
        from teatree.loops.dream.compliance import render_compliance_show  # noqa: PLC0415 — lazy command import

        for line in render_compliance_show():
            self.stdout.write(line)

    def _run_pass(
        self,
        *,
        since: dt.datetime | None,
        dry_run: bool,
        enforce_cadence: bool,
        mode: PipelineMode = _DEFAULT_MODE,
    ) -> PassOutcome:
        import os  # noqa: PLC0415 — deferred: loaded only when this command runs

        from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

        from teatree.core.models import DreamRunMarker, Loop, LoopLease  # noqa: PLC0415 — deferred: ORM/app-registry
        from teatree.loops.dream import lease  # noqa: PLC0415 — deferred: keeps command import light
        from teatree.loops.dream.loop import (  # noqa: PLC0415 — deferred: keeps command import light
            DREAM_LEASE_NAME,
            DREAM_LEASE_SECONDS,
            DREAM_PASS_BUDGET_SECONDS,
            DREAM_RETRY_BACKOFF_SECONDS,
            DREAM_TAIL_RESERVE_SECONDS,
            MINI_LOOP,
        )
        from teatree.loops.enable_verdict import loop_admits  # noqa: PLC0415 — deferred: keeps command import light

        now = timezone.now()
        if enforce_cadence:
            # The ONE cadence ledger: the dream Loop row's is_due / last_run_at, gated
            # by the single enable verdict (Loop.enabled + LoopState) — never a second
            # cadence ledger. dream is off_live_tick, so the live tick never bumps
            # this row; t3 dream tick owns its last_run_at alone.
            row = Loop.objects.filter(name=MINI_LOOP.name).first()
            if row is None or not loop_admits(MINI_LOOP.name):
                self.stdout.write("SKIP  dream loop disabled (no enabled Loop row / LoopState hold).")
                return PassOutcome.SKIPPED
            if not row.is_due(now):
                self.stdout.write("SKIP  dream cadence not elapsed.")
                return PassOutcome.SKIPPED
            # Cadence alone cannot bound the RETRY rate. `is_due` reads `last_run_at`,
            # which only a STAMPED pass bumps (#2285 keeps a failed pass retrying rather
            # than waiting out the full day) — so a pass that ends without stamping
            # leaves the loop due and the 600s driver chain relaunches it on its very
            # next fire, forever. `last_attempt_at` is the anchor no pass may withhold:
            # it is stamped below BEFORE the pass, so a SIGKILLed pass still moves it.
            backoff_left = _retry_backoff_remaining(row.last_attempt_at, now, DREAM_RETRY_BACKOFF_SECONDS)
            if backoff_left > 0:
                self.stdout.write(
                    f"SKIP  dream retry backoff — {backoff_left / 60:.0f} min left of the "
                    f"{DREAM_RETRY_BACKOFF_SECONDS / 3600:.0f}h floor since the last attempt.",
                )
                return PassOutcome.SKIPPED

        owner = lease.lease_owner(os.getpid())
        verdict = lease.acquire(owner=owner, lease_seconds=DREAM_LEASE_SECONDS)
        if verdict.message:
            self.stdout.write(verdict.message)
        if not verdict.acquired:
            return PassOutcome.SKIPPED

        # BEFORE the pass, not after: the observed failure is a pass SIGKILLed at its
        # 1800s deadline, which never reaches a line below this one. The liveness anchor
        # has to survive that or the loop still reads as never driven (#4355).
        if enforce_cadence:
            Loop.objects.mark_attempted(MINI_LOOP.name, now)

        # The pass's wall clock opens AFTER the lease is won, because a pass that
        # SKIPped never spent any of it. Everything metered inside the pass is measured
        # against this, and `tail_reserve` is what it may not spend.
        budget = PassBudget.start(total=DREAM_PASS_BUDGET_SECONDS, tail_reserve=DREAM_TAIL_RESERVE_SECONDS)
        resolved = replace(mode, propose_evals=mode.propose_evals or _env_propose_evals())
        try:
            outcome = self._consolidate_and_mark(since=since, dry_run=dry_run, now=now, mode=resolved, budget=budget)
        finally:
            LoopLease.objects.release(DREAM_LEASE_NAME, owner=owner)

        # The cadence anchor stays gated on success — a pass that stamped nothing is retried
        # on the next driver fire rather than waiting out the full day (#2285).
        if enforce_cadence and outcome is PassOutcome.STAMPED:
            Loop.objects.mark_run(MINI_LOOP.name, now)

        # Re-read confirmation so a stamped success can be cited (resilience #7).
        if not dry_run:
            marker = DreamRunMarker.objects.filter(name=DreamRunMarker.NAME).first()
            stamped = marker.last_succeeded_at.isoformat() if marker and marker.last_succeeded_at else "none"
            self.stdout.write(f"      dream marker last_succeeded_at={stamped}")

        return outcome

    def _consolidate_and_mark(
        self,
        *,
        since: dt.datetime | None,
        dry_run: bool,
        now: dt.datetime,
        mode: PipelineMode = _DEFAULT_MODE,
        budget: PassBudget | None = None,
    ) -> PassOutcome:
        from teatree.core.models import DreamRunMarker  # noqa: PLC0415 — deferred: ORM import needs the app registry
        from teatree.loops.dream import engine  # noqa: PLC0415 — deferred: keeps command import light
        from teatree.loops.dream.eval_proposer import EvalProposalRequest  # noqa: PLC0415 — lazy command import

        request = EvalProposalRequest() if mode.propose_evals else None
        try:
            result = engine.run_consolidation(
                overlay="",
                since=since,
                dry_run=dry_run,
                eval_proposals=request,
                distill=engine.DistillPolicy(budget=budget),
            )
        except Exception as exc:  # noqa: BLE001 — a dream pass failure is marked attempted + reported, never crashes the command
            if not dry_run:
                DreamRunMarker.objects.mark_attempted(now)
            self.stdout.write(f"FAIL  dream pass raised: {type(exc).__name__}: {exc}")
            return PassOutcome.FAILED

        clauses = _ResultFragments.of(result)

        # A broken/raised batch means PART of the corpus was not folded in, so the pass
        # may never stamp success — but it must not short-circuit the rest of the pass
        # either. `write_clusters` has ALREADY persisted the rows the HEALTHY batches
        # produced by the time this is known, so returning here left those rows behind and
        # skipped the very phases that reconcile them: cross-link, re-index, decay, and the
        # §4 acceptance gates. Report it loudly and carry on; the marker is withheld at the
        # bottom, where every other "do not stamp" verdict is already decided.
        if result.distillation_broken:
            self._report_broken_distillation(result)

        if dry_run:
            self.stdout.write(
                f"DRY   dream pass — {result.clusters_recorded} cluster(s) would be recorded "
                f"from {result.members_replayed} member(s){clauses.distilled}{clauses.evals}"
                f"{clauses.empty}{clauses.rejected}{clauses.deferred}{clauses.budget_stopped}{clauses.broken}; "
                "no rows or marker written.",
            )
            # A preview wrote nothing, so there are no rows to reconcile — the carry-on
            # above buys nothing here, and the outcome stays what it has always been.
            return PassOutcome.FAILED if result.distillation_broken else PassOutcome.DRY_RUN

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
            return PassOutcome.FAILED

        # ONE budget for the whole pass, shared by every promoting phase — three phases
        # each granted the full cap would triple it (#4176). Named apart from the pass's
        # WALL-CLOCK budget above: two different bounds on the same pass.
        promotion = PromotionBudget.from_config()
        phases = self._gap_phases()
        promoted = self._promote_candidates(
            propose_evals=mode.propose_evals,
            dry_run=dry_run,
            force_all_phases=mode.force_all_phases,
            validate_live=mode.validate_live,
        )
        # Phase 3c (#2663) runs BEFORE the gates so gate (g) reads the just-persisted
        # compliance records (a recurrence remediated with a memory FAILS the pass).
        # Measurement is the root KPI — it runs on EVERY pass (default ON) and reuses the
        # extract the engine already built; escalation is the default-OFF other half.
        compliance = phases.run_compliance(extract=result.extract, dry_run=dry_run, budget=promotion)
        # Phase 3d (#2663) — the "improve-with-new-stuff" sibling: promote recurring
        # automatable user asks to a fix-and-merge under the same standing umbrella.
        automation_asks = phases.run_automation_asks(
            extract=result.extract, dry_run=dry_run, force_all_phases=mode.force_all_phases, budget=promotion
        )
        memory_phases, gates_passed, gates_summary = self._run_memory_phases_and_gates(
            clusters_recorded=result.clusters_recorded, dry_run=dry_run
        )
        memory_promote = phases.run_memory_promotion(
            dry_run=dry_run, force_all_phases=mode.force_all_phases, budget=promotion
        )
        deferred = promotion.summary

        # The §4 acceptance gates make the pass anti-vacuous: a lossy / delete-only
        # / no-op consolidation FAILS a gate, and a failing gate must NOT stamp
        # success — staleness keeps firing until a faithful pass lands (#2545). A
        # broken/raised distiller batch joins them here rather than short-circuiting
        # above, so the maintenance phases still run over the rows that WERE written
        # while the marker is still withheld.
        if not gates_passed or result.distillation_broken:
            reason = "acceptance gate(s) FAILED" if not gates_passed else "the distiller FAILED on some batch(es)"
            DreamRunMarker.objects.mark_attempted(now)
            self.stdout.write(
                f"WARN  dream pass — {result.clusters_recorded} cluster(s) recorded "
                f"from {result.members_replayed} member(s){clauses.distilled}{clauses.evals}"
                f"{clauses.empty}{clauses.rejected}"
                f"{promoted}{compliance}{automation_asks}"
                f"{memory_phases}{memory_promote}{deferred}{gates_summary}{clauses.deferred}"
                f"{clauses.budget_stopped}{clauses.broken}; "
                f"{reason} — marker NOT stamped succeeded.",
            )
            return PassOutcome.FAILED

        DreamRunMarker.objects.mark_succeeded(now)
        self.stdout.write(
            f"OK    dream pass — {result.clusters_recorded} cluster(s) recorded "
            f"from {result.members_replayed} member(s){clauses.distilled}{clauses.evals}"
            f"{clauses.empty}{clauses.rejected}"
            f"{promoted}{compliance}{automation_asks}"
            f"{memory_phases}{memory_promote}{deferred}{gates_summary}{clauses.deferred}{clauses.budget_stopped}.",
        )
        return PassOutcome.STAMPED

    def _report_broken_distillation(self, result: "DreamRunResult") -> None:
        """Print WHY the distiller produced nothing, quoting the reply it could not parse.

        The reply is the actionable part: ``Not logged in · Please run /login`` names an
        auth gap the operator fixes in one step, where a bare ``unparsable`` reads like a
        model formatting slip and cost a full debugging session to tell apart.

        Says "on N batch(es)", not "NO consolidation happened": the failure is per-batch,
        the healthy batches' clusters are already in the ledger, and the pass carries on
        through the maintenance phases that reconcile them. The marker is still withheld.
        """
        self.stdout.write(
            f"FAIL  dream distiller could not do its job on "
            f"{result.broken_batches} broken + {result.failed_batches} raised batch(es) "
            f"over {result.snippets_distilled} snippet(s); those batches were NOT consolidated, "
            "the distill cursor is HELD so they are re-reached next pass, and the marker is "
            "NOT stamped succeeded.",
        )
        for line in result.distill_diagnostics:
            self.stdout.write(f"      {line}")

    def _promote_candidates(
        self, *, propose_evals: bool, dry_run: bool, force_all_phases: bool = False, validate_live: bool = False
    ) -> str:
        """Promote the freshly-derived candidates to live scenarios (guarded; never raises).

        Runs only when proposals were requested. Each candidate clears the
        NON-BYPASSABLE anti-vacuity guard
        (:func:`teatree.loops.dream.promote.guard_can_fail`) AND a live-model pass@k
        before it lands. *validate_live* (``--validate-live`` / ``--full``, or the
        default-OFF ``validate_live`` toggle on the cron path) supplies the real METERED
        validator (:func:`promote.build_live_validator`); WITHOUT it nothing auto-lands —
        every clearing candidate is WITHHELD, the nightly ``tick``'s default. A promotion failure is reported in
        the summary line, never crashing the pass that already stamped success. When
        the default-OFF LLM derivation (#2447) is enabled, each candidate is
        additionally synthesized into a full scenario and STAGED (never auto-committed).
        """
        if not propose_evals:
            return ""
        try:
            from teatree.loops.dream import promote  # noqa: PLC0415 — deferred: keeps command import light
            from teatree.loops.dream.eval_proposer import _default_proposals_path  # noqa: PLC0415 — lazy command import

            validator = promote.build_live_validator() if validate_live else None
            outcomes = promote.promote_proposals_file(
                _default_proposals_path(), dry_run=dry_run, live_gate=promote.LiveGate(validator=validator)
            )
        except Exception as exc:  # noqa: BLE001 — an eval-promotion failure degrades to a WARN clause
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
        from teatree.loops.dream.loop import derive_evals_enabled  # noqa: PLC0415 — deferred: lazy command import

        if not force_all_phases and not derive_evals_enabled():
            return ""
        try:
            from teatree.loops.dream import llm_eval_proposer  # noqa: PLC0415 — deferred: keeps command import light
            from teatree.loops.dream.eval_proposer import _default_proposals_path  # noqa: PLC0415 — lazy command import

            outcomes = llm_eval_proposer.stage_proposals_file(_default_proposals_path(), dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001 — an eval-derivation failure degrades to a WARN clause
            return f"; WARN eval derivation raised: {type(exc).__name__}: {exc}"
        if not outcomes:
            return ""
        staged = sum(1 for o in outcomes if o.derived)
        return f"; staged {staged} derived eval(s) for review, dropped {len(outcomes) - staged}"

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

    def _gap_phases(self) -> "GapPromotionPhases":
        """The gap-promoting phases (3c/3d/Pass-2), wired to the backlog host resolver."""
        from teatree.loops.dream.gap_phases import GapPromotionPhases  # noqa: PLC0415 — deferred: lazy command import

        return GapPromotionPhases(backlog_host_resolver=self._teatree_backlog_host)

    def _phase_runner(self) -> "MemoryPhaseRunner":
        """The composed file-side phase runner, wired to the backlog host resolver."""
        from teatree.loops.dream.phase_runner import MemoryPhaseRunner  # noqa: PLC0415 — deferred: lazy command import

        return MemoryPhaseRunner(backlog_host_resolver=self._teatree_backlog_host)

    def _run_memory_phases(self, *, dry_run: bool) -> str:
        """Run phases 4-6 over every discovered memory dir (quiet-night path, no gates)."""
        return self._phase_runner().run_memory_phases(dry_run=dry_run)

    def _run_memory_phases_and_gates(self, *, clusters_recorded: int, dry_run: bool) -> "tuple[str, bool, str]":
        """Run phases 4-6 then the §4 acceptance gates, gating success on the gates (#2545)."""
        return self._phase_runner().run_memory_phases_and_gates(clusters_recorded=clusters_recorded, dry_run=dry_run)


def _retry_backoff_remaining(last_attempt_at: dt.datetime | None, now: dt.datetime, backoff: float) -> float:
    """Seconds still owed to the retry backoff — ``0`` (or less) when a retry may run.

    A loop that has never attempted has nothing to back off from, so it runs.
    """
    if last_attempt_at is None:
        return 0.0
    return backoff - (now - last_attempt_at).total_seconds()


def _surface_exit_code(outcome: PassOutcome) -> None:
    """Turn a pass outcome into the process exit code (#3993).

    ``SystemExit``, never ``typer.Exit``: these commands are reached through
    ``call_command``, which swallows ``typer.Exit`` and exits 0 on a real failure
    (AGENTS.md § "Deciding Where a New Command Lives").
    """
    if outcome is PassOutcome.FAILED:
        raise SystemExit(1)


def _env_propose_evals() -> bool:
    """Resolve the ``dream_propose_evals`` opt-in for the manual ``run`` path.

    The manual ``run`` enables the eval phase when ``--propose-evals`` is given OR
    this setting is on. DB-home (#1775): resolved via the effective-settings tier
    (set with ``t3 <overlay> config_setting set dream_propose_evals true``). The
    cadence-driven ``tick`` path does NOT route through here — it resolves the
    seam (LIVE by default, env/toml kill-switch) via
    :func:`teatree.loops.dream.loop.propose_evals_enabled`.
    """
    from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps command import light

    return get_effective_settings().dream_propose_evals


def _parse_since(raw: str) -> dt.datetime | None:
    """Parse the ``--since`` ISO-8601 string; empty → ``None`` (engine default).

    A naive value (``--since 2026-06-01``) is normalized to the current
    timezone so the ``USE_TZ`` engine never compares naive against aware. A
    malformed value raises ``CommandError`` instead of a raw traceback.
    """
    from django.core.management.base import CommandError  # noqa: PLC0415 — deferred: Django import at call time
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

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
