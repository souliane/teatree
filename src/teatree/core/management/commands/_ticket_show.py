"""``ticket show`` + ``ticket expedite`` — read/set a ticket's state (#2009, PR-07).

Split out of ``ticket.py`` as a :class:`TicketShowCommands` mixin (the same MRO
split as ``RubricCommands``) so the already-cap-bound command god-module does not
grow. Holds ``show`` (per-phase ``attempt N/max`` budget over the ticket's
``TaskAttempt`` rows) and ``expedite`` (the release-blocker flag).
"""

from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command

from teatree.core.modelkit.phases import normalize_phase
from teatree.core.models import TaskAttempt, Ticket
from teatree.core.repair_loop import max_phase_iterations


class PhaseBudgetRow(TypedDict):
    phase: str
    attempts: int
    max: int


class TicketShowResult(TypedDict):
    ticket_id: int
    state: str
    overlay: str
    issue_url: str
    expedited: bool
    phases: list[PhaseBudgetRow]


class ExpediteResult(TypedDict, total=False):
    ticket_id: int
    expedited: bool


def phase_budget_rows(ticket: Ticket) -> list[PhaseBudgetRow]:
    """Per-phase ``(attempts, max)`` budget for *ticket*, ordered by first attempt.

    Attempts are grouped by the canonical phase token so a short-verb ``review``
    and gerund ``reviewing`` collapse into one row, matching how the repair-loop
    counts a ticket-phase. ``max`` is the configured iteration cap.
    """
    cap = max_phase_iterations()
    counts: dict[str, int] = {}
    first_pk: dict[str, int] = {}
    for attempt in TaskAttempt.objects.filter(task__ticket_id=ticket.pk).select_related("task").order_by("pk"):
        phase = normalize_phase(attempt.task.phase) or "(none)"
        counts[phase] = counts.get(phase, 0) + 1
        first_pk.setdefault(phase, attempt.pk)
    return [
        PhaseBudgetRow(phase=phase, attempts=counts[phase], max=cap)
        for phase in sorted(counts, key=lambda p: first_pk[p])
    ]


def render_ticket_show(result: TicketShowResult) -> str:
    """Render a terse human-readable ``ticket show`` block from *result*."""
    lines = [
        f"Ticket #{result['ticket_id']} [{result['state']}]",
        f"  overlay: {result['overlay'] or '-'}",
        f"  issue:   {result['issue_url'] or '-'}",
    ]
    if result.get("expedited"):
        lines.append("  expedite: ⚡ release-blocker (pre-CI push allowed; merge still gated on review+test)")
    if not result["phases"]:
        lines.append("  phases:  (no attempts yet)")
        return "\n".join(lines)
    lines.append("  phases:")
    lines.extend(f"    {row['phase']}: attempt {row['attempts']}/{row['max']}" for row in result["phases"])
    return "\n".join(lines)


class TicketShowCommands(TyperCommand):
    """The ``ticket show`` command, mounted via MRO inheritance (#2009).

    Lives here as a mixin the ``ticket`` command inherits (the same split as
    ``RubricCommands``) so its LOC stays out of the already-cap-bound
    ``ticket.py``. django-typer collects ``@command`` methods from every
    ``TyperCommand`` base in the MRO, so the CLI surface is unchanged.
    """

    @command()
    def show(self, ticket_id: int) -> TicketShowResult:
        """Show a ticket's state plus the per-phase ``attempt N/max`` budget (#2009).

        Surfaces the visible repair-loop iteration budget: for each phase the
        ticket has attempted, ``attempt <count>/<cap>`` against the configurable
        ``MAX_PHASE_ITERATIONS`` cap, so the operator can see how much of the
        retry budget each phase has burned before the re-queue chokepoint refuses
        with ``MaxIterationsExceeded``.
        """
        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  Ticket {ticket_id} not found")
            raise SystemExit(1) from None
        result: TicketShowResult = {
            "ticket_id": int(ticket.pk),
            "state": ticket.state,
            "overlay": ticket.overlay,
            "issue_url": ticket.issue_url,
            "expedited": ticket.expedited,
            "phases": phase_budget_rows(ticket),
        }
        self.stdout.write(render_ticket_show(result))
        return result

    @command()
    def expedite(
        self,
        ticket_id: int,
        *,
        off: Annotated[bool, typer.Option("--off", help="Clear the flag instead of setting it.")] = False,
    ) -> ExpediteResult:
        """Flag a ticket as expedite/release-blocker (``--off`` clears it) (PR-07).

        A flagged ticket may push before CI completes; the merge keystone is NEVER
        relaxed — merge stays gated on local review + test evidence. Surfaces on
        ``ticket show`` and as a ⚡ statusline chip.
        """
        try:
            ticket = Ticket.objects.get(pk=ticket_id)
        except Ticket.DoesNotExist:
            self.stderr.write(f"  Ticket {ticket_id} not found")
            raise SystemExit(1) from None
        ticket.expedited = not off
        ticket.save(update_fields=["expedited"])
        self.stdout.write(f"  {'expedited' if ticket.expedited else 'cleared expedite on'} ticket {ticket.pk}")
        return {"ticket_id": int(ticket.pk), "expedited": ticket.expedited}
