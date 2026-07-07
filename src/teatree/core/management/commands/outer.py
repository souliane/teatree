"""``manage.py outer`` — drive the T4 autoresearch outer-loop cron + operator verbs.

Mirrors the ``dream`` off-live-tick cron shape: ``tick`` runs one guarded step
only when the ``outer_loop`` :class:`Loop` row is enabled AND its cadence has
elapsed (the ONE ``is_due`` / ``last_run_at`` ledger), behind the in-flight
``outer-loop-tick`` lease so two ticks never overlap. ``status`` / ``history`` are
read-only; ``propose`` records an operator hypothesis (refused while the flag is
off, so the surface stays inert at defaults). All experiment mutation flows
through :func:`teatree.loops.outer_loop.tick.run_tick` and the guarded
``OuterLoopExperiment`` helpers — this command owns only the cron mechanics.
"""

import os
from typing import Annotated

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command

from teatree.config import get_effective_settings
from teatree.core.models import Loop, LoopLease, OuterLoopExperiment

# The teatree.loops / teatree.loop imports below are DEFERRED to the method body on
# purpose: a top-level teatree.loops import from a teatree.core management command is a
# module-level import cycle (loops depends on core). They resolve fine at call time.


class Command(TyperCommand):
    help = "Drive the T4 autoresearch outer loop (propose→ratify→implement→measure→keep-only-if-better)."

    @command(name="tick")
    def tick(self) -> None:
        """Advance the outer loop one step IF the cadence elapsed (cron entry)."""
        from teatree.loop.loop_state_db import loop_enabled  # noqa: PLC0415 — cross-layer import cycle
        from teatree.loops.outer_loop.loop import (  # noqa: PLC0415 — cross-layer import cycle
            MINI_LOOP,
            OUTER_LOOP_LEASE_NAME,
            OUTER_LOOP_LEASE_SECONDS,
        )
        from teatree.loops.outer_loop.tick import run_tick  # noqa: PLC0415 — cross-layer import cycle

        now = timezone.now()
        row = Loop.objects.filter(name=MINI_LOOP.name).first()
        if row is None or not loop_enabled(MINI_LOOP.name):
            self.stdout.write("SKIP  outer_loop disabled (no enabled Loop row / LoopState hold).")
            return
        if not row.is_due(now):
            self.stdout.write("SKIP  outer_loop cadence not elapsed.")
            return

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire(OUTER_LOOP_LEASE_NAME, owner=owner, lease_seconds=OUTER_LOOP_LEASE_SECONDS):
            self.stdout.write("SKIP  another outer_loop tick is already running — lease held.")
            return
        try:
            result = run_tick(now=now)
        finally:
            LoopLease.objects.release(OUTER_LOOP_LEASE_NAME, owner=owner)
        Loop.objects.mark_run(MINI_LOOP.name, now)
        detail = f" ({result.reason})" if result.reason else ""
        experiment = f" experiment={result.experiment_id}" if result.experiment_id else ""
        self.stdout.write(f"OK    outer_loop tick — {result.action}{detail}{experiment}.")

    @command(name="status")
    def status(self) -> None:
        """Print the guard-chain verdict and the active experiment (read-only)."""
        from teatree.loops.outer_loop.guards import evaluate_guards  # noqa: PLC0415 — cross-layer import cycle

        verdict = evaluate_guards(settings=get_effective_settings())
        gate = "ALLOW" if verdict.ok else f"REFUSE ({verdict.reason})"
        self.stdout.write(f"outer_loop guard chain: {gate}")
        active = OuterLoopExperiment.objects.active().order_by("created_at", "pk").first()
        if active is None:
            self.stdout.write("  no active experiment.")
            return
        self.stdout.write(f"  active experiment #{active.pk}: state={active.state} target={active.target_provider_id}")

    @command(name="propose")
    def propose(
        self,
        *,
        hypothesis: Annotated[str, typer.Option("--hypothesis", help="The operator hypothesis to test.")] = "",
        target: Annotated[str, typer.Option("--target", help="The signal provider_id to improve.")] = "",
    ) -> None:
        """Record an operator hypothesis as a PROPOSED experiment (refused while off)."""
        from teatree.loops.outer_loop.propose import operator_proposal  # noqa: PLC0415 — cross-layer import cycle

        if not get_effective_settings().outer_loop_enabled:
            self.stderr.write("  refusing: outer_loop_enabled is off (the shipped OFF state).")
            raise SystemExit(2)
        if not hypothesis.strip() or not target.strip():
            self.stderr.write("  --hypothesis and --target are both required.")
            raise SystemExit(1)
        candidate = operator_proposal(hypothesis.strip(), target.strip())
        experiment = OuterLoopExperiment.objects.propose(candidate)
        self.stdout.write(f"proposed experiment #{experiment.pk} (state={experiment.state}).")

    @command(name="resolve-revert")
    def resolve_revert(
        self,
        experiment_id: int,
        *,
        revert_sha: Annotated[str, typer.Option("--revert-sha", help="The git revert commit sha (provenance).")] = "",
    ) -> None:
        """Close a REVERT_PENDING experiment to terminal REVERTED, freeing the slot."""
        from teatree.loops.outer_loop.revert import resolve_revert  # noqa: PLC0415 — cross-layer import cycle

        experiment = OuterLoopExperiment.objects.filter(pk=experiment_id).first()
        if experiment is None:
            self.stderr.write(f"  no experiment #{experiment_id}.")
            raise SystemExit(1)
        if experiment.state != OuterLoopExperiment.State.REVERT_PENDING:
            self.stderr.write(f"  experiment #{experiment_id} is {experiment.state}, not revert_pending.")
            raise SystemExit(1)
        resolve_revert(experiment, revert_sha=revert_sha.strip())
        self.stdout.write(f"reverted experiment #{experiment.pk} (state={experiment.state}).")

    @command(name="resolve-keep")
    def resolve_keep(self, experiment_id: int) -> None:
        """Close a KEEP_PENDING experiment to terminal KEPT, freeing the slot."""
        from teatree.loops.outer_loop.keep import resolve_keep  # noqa: PLC0415 — cross-layer import cycle

        experiment = OuterLoopExperiment.objects.filter(pk=experiment_id).first()
        if experiment is None:
            self.stderr.write(f"  no experiment #{experiment_id}.")
            raise SystemExit(1)
        if experiment.state != OuterLoopExperiment.State.KEEP_PENDING:
            self.stderr.write(f"  experiment #{experiment_id} is {experiment.state}, not keep_pending.")
            raise SystemExit(1)
        resolve_keep(experiment)
        self.stdout.write(f"kept experiment #{experiment.pk} (state={experiment.state}).")

    @command(name="history")
    def history(
        self,
        *,
        limit: Annotated[int, typer.Option("--limit", help="How many recent experiments to show.")] = 10,
    ) -> None:
        """Print the recent experiment ledger (read-only)."""
        rows = OuterLoopExperiment.objects.all().order_by("-created_at", "-pk")[: max(limit, 1)]
        if not rows:
            self.stdout.write("no experiments recorded.")
            return
        for exp in rows:
            decision = f" decision={exp.decision}" if exp.decision else ""
            self.stdout.write(f"  #{exp.pk} {exp.state} target={exp.target_provider_id} source={exp.source}{decision}")
