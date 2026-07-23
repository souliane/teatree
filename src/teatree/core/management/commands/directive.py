"""``manage.py directive`` — capture, drive, and inspect directives (north-star PR-6 + PR-7).

``capture`` records a plain-language directive as a ``CAPTURED`` :class:`Directive`
verbatim — always available, even while the loop is dark, because it is the EXPLICIT
operator path (the ``DIRECTIVE``-intent router stays parity-off until
``directive_loop_enabled`` is on). ``tick`` (PR-7) is the off-live-tick cron entry: it
advances the oldest active directive ONE guarded FSM step only when the
``directive_loop`` :class:`Loop` row is enabled AND its cadence has elapsed, behind the
``directive-loop-tick`` lease. ``resolve-revert`` closes a ``REVERT_PENDING`` directive
to terminal ``REVERTED`` (its overlay config is already rolled back). ``list`` /
``status`` / ``history`` are read-only. All directive mutation flows through
:func:`teatree.loops.directive_loop.tick.run_tick` and the guarded ``Directive`` helpers.
"""

import os
from typing import Annotated

import typer
from django.utils import timezone
from django_typer.management import TyperCommand, command

from teatree.core.models import Directive, Loop, LoopLease

# The teatree.loops imports below are DEFERRED to the method body: a top-level
# teatree.loops import from a teatree.core management command is a module-level import
# cycle (loops depends on core). They resolve fine at call time.


class Command(TyperCommand):
    help = "Capture, drive, and inspect plain-language directives about teatree's own behavior."

    @command(name="capture")
    def capture(
        self,
        text: str,
        *,
        scope: Annotated[
            str, typer.Option("--scope", help="The overlay the directive is scoped to (blank = global).")
        ] = "",
    ) -> None:
        """Record a plain-language directive verbatim as a CAPTURED row."""
        if not text.strip():
            self.stderr.write("  directive text is required and must be non-empty.")
            raise SystemExit(1)
        directive = Directive.objects.capture(text, source=Directive.Source.CLI, scope_overlay=scope.strip())
        self.stdout.write(f"captured directive #{directive.pk} (state={directive.state}).")

    @command(name="list")
    def list_directives(
        self,
        *,
        limit: Annotated[int, typer.Option("--limit", help="How many recent directives to show.")] = 20,
    ) -> None:
        """Print the recent directive ledger (read-only)."""
        rows = Directive.objects.all().order_by("-created_at", "-pk")[: max(limit, 1)]
        if not rows:
            self.stdout.write("no directives recorded.")
            return
        for directive in rows:
            scope = directive.scope_overlay or "<global>"
            self.stdout.write(f"  #{directive.pk} {directive.state} scope={scope} — {directive.raw_text[:60]}")

    @command(name="status")
    def status(self, directive_id: int) -> None:
        """Print one directive's state, sketch, and ratification (read-only)."""
        directive = Directive.objects.filter(pk=directive_id).first()
        if directive is None:
            self.stderr.write(f"  no directive #{directive_id}.")
            raise SystemExit(1)
        scope = directive.scope_overlay or "<global>"
        self.stdout.write(
            f"directive #{directive.pk}: state={directive.state} gen={directive.generation} scope={scope}"
        )
        self.stdout.write(f"  text: {directive.raw_text}")
        if directive.constraint_statement:
            self.stdout.write(f"  constraint: {directive.constraint_statement}")
        sketch = directive.sketch
        if sketch is not None:
            self.stdout.write(
                f"  sketch: kind={sketch.kind} setting={sketch.setting_key} chokepoint={sketch.policy_chokepoint}"
            )
            self.stdout.write(f"  rejected alternatives: {'; '.join(sketch.rejected_alternatives)}")
        if directive.ratify_question_id is not None:
            answered = "answered" if directive.ratify_question and directive.ratify_question.answered_at else "pending"
            self.stdout.write(f"  ratify question: {answered}")
        if directive.decision_reason:
            self.stdout.write(f"  decision: {directive.decision_reason}")

    @command(name="tick")
    def tick(self) -> None:
        """Advance the directive loop one step IF the cadence elapsed (cron entry)."""
        from teatree.loop.loop_state_db import loop_enabled  # noqa: PLC0415 — cross-layer import cycle
        from teatree.loops.directive_loop.loop import (  # noqa: PLC0415 — cross-layer import cycle
            DIRECTIVE_LOOP_LEASE_NAME,
            DIRECTIVE_LOOP_LEASE_SECONDS,
            MINI_LOOP,
        )
        from teatree.loops.directive_loop.tick import run_tick  # noqa: PLC0415 — cross-layer import cycle

        now = timezone.now()
        row = Loop.objects.filter(name=MINI_LOOP.name).first()
        if row is None or not loop_enabled(MINI_LOOP.name):
            self.stdout.write("SKIP  directive_loop disabled (no enabled Loop row / LoopState hold).")
            return
        if not row.is_due(now):
            self.stdout.write("SKIP  directive_loop cadence not elapsed.")
            return

        owner = f"pid-{os.getpid()}"
        if not LoopLease.objects.acquire(
            DIRECTIVE_LOOP_LEASE_NAME, owner=owner, lease_seconds=DIRECTIVE_LOOP_LEASE_SECONDS
        ):
            self.stdout.write("SKIP  another directive_loop tick is already running — lease held.")
            return
        try:
            result = run_tick(now=now)
        finally:
            LoopLease.objects.release(DIRECTIVE_LOOP_LEASE_NAME, owner=owner)
        Loop.objects.mark_run(MINI_LOOP.name, now)
        detail = f" ({result.reason})" if result.reason else ""
        directive = f" directive={result.directive_id}" if result.directive_id else ""
        # A guard refusal is a distinct outcome from a healthy tick, never an "OK" (#3643).
        prefix = "WARN " if result.action == "refused" else "OK   "
        self.stdout.write(f"{prefix} directive_loop tick — {result.action}{detail}{directive}.")

    @command(name="resolve-revert")
    def resolve_revert(
        self,
        directive_id: int,
        *,
        revert_sha: Annotated[str, typer.Option("--revert-sha", help="The git revert commit sha (provenance).")] = "",
    ) -> None:
        """Close a REVERT_PENDING directive to terminal REVERTED (config already rolled back)."""
        from teatree.loops.directive_loop.revert import resolve_revert  # noqa: PLC0415 — cross-layer import cycle

        directive = Directive.objects.filter(pk=directive_id).first()
        if directive is None:
            self.stderr.write(f"  no directive #{directive_id}.")
            raise SystemExit(1)
        if directive.state != Directive.State.REVERT_PENDING:
            self.stderr.write(f"  directive #{directive_id} is {directive.state}, not revert_pending.")
            raise SystemExit(1)
        resolve_revert(directive, revert_sha=revert_sha.strip())
        self.stdout.write(f"reverted directive #{directive.pk} (state={directive.state}).")

    @command(name="history")
    def history(
        self,
        *,
        limit: Annotated[int, typer.Option("--limit", help="How many recent directives to show.")] = 10,
    ) -> None:
        """Print the recent directive ledger with decisions (read-only)."""
        rows = Directive.objects.all().order_by("-created_at", "-pk")[: max(limit, 1)]
        if not rows:
            self.stdout.write("no directives recorded.")
            return
        for directive in rows:
            reason = f" — {directive.decision_reason}" if directive.decision_reason else ""
            self.stdout.write(f"  #{directive.pk} {directive.state} gen={directive.generation}{reason}")
