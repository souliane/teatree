"""``pr create`` ticket-resolution concern (split out of ``pr.py``).

Pure, command-class-free helpers that turn the CLI ``ticket_id``
argument (pk / issue number / issue URL) into a ``Ticket`` — or, when
no canonical row exists, an actionable structured error instead of a
bare ``Ticket.DoesNotExist`` (#1051). Kept as a sibling module (same
pattern as ``_pr_preview.py`` / ``_ship/exec.py``) so ``pr.py`` stays
within the module-health LOC budget and the "ticket resolution" concern
is named by its own file (self-documenting hierarchy).
"""

from typing import TypedDict

from teatree.core.models import Ticket


class TicketNotFoundError(TypedDict):
    error: str
    hint: str


def resolve_ticket(ref: str) -> Ticket:
    """Resolve a ticket by pk / issue number / issue URL.

    Thin wrapper over ``Ticket.objects.resolve`` — the shared resolver so
    ``pr create`` and ``lifecycle visit-phase`` accept the same identifier
    set (#694).
    """
    return Ticket.objects.resolve(ref)


def ticket_not_found_error(ref: str) -> TicketNotFoundError:
    """Actionable result for a ``pr create`` with no canonical Ticket row (#1051).

    The autonomous-loop case: a branch + PR exist for an issue whose
    Ticket row was never created (work done outside the FSM) or was
    pruned. ``Ticket.objects.resolve`` raises a bare
    ``Ticket.DoesNotExist``; pre-#1051 that propagated uncaught and the
    implementer fell back to a manual ``gh pr create``, bypassing
    overlay-managed PR invariants (title format, FSM transitions,
    on-behalf gates). Name the missing reference and the command that
    provisions the row instead.
    """
    hint = f"t3 <overlay> workspace ticket <issue-url> (no Ticket row for {ref!r})"
    return TicketNotFoundError(
        error=(
            f"No Ticket row for {ref!r} in the canonical DB. "
            f"Create one with `t3 <overlay> workspace ticket <issue-url>` "
            f"(or pass the internal DB pk)."
        ),
        hint=hint,
    )
