"""``manage.py directive`` — capture + inspect directives (north-star PR-6 intake surface).

The deterministic day-one intake path (§3.3): ``capture`` records a plain-language
directive as a ``CAPTURED`` :class:`Directive` verbatim — always available, even
while the loop is dark, because it is the EXPLICIT operator path (the ``DIRECTIVE``-
intent router stays parity-off until ``directive_loop_enabled`` is on). ``list`` and
``status`` are read-only. The interpret → ratify → admit advance is the directive
loop's (a later PR); this command owns only intake and inspection.
"""

from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.core.models import Directive


class Command(TyperCommand):
    help = "Capture and inspect plain-language directives about teatree's own behavior."

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
