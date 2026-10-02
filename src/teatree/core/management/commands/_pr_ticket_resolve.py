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

from teatree.core.management.commands._pr_control_db import (
    ControlDbUnreachableError,
    control_db_unreachable_error,
    unreachable_control_db_reason,
)
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

    #4170: both supported routes are named, because the refusal reads as a dead end
    otherwise and a dead end is what sends the next agent to ``gh pr create``. Either
    provision the row and ship through the FSM, or open the PR ticketlessly via the
    orphan-branch path — ``pr create`` itself requires a ticket by design, since its
    gates are keyed on the row.
    """
    hint = f"t3 <overlay> workspace ticket <issue-url> (no Ticket row for {ref!r})"
    return TicketNotFoundError(
        error=(
            f"No Ticket row for {ref!r} in the canonical DB. "
            f"Create one with `t3 <overlay> workspace ticket <issue-url>` "
            f"(or pass the internal DB pk). To open a PR with no ticket at all, use "
            f"`t3 <overlay> pr ensure-pr --repo <path> --branch <branch>`."
        ),
        hint=hint,
    )


def resolve_ticket_or_refusal(ref: str) -> Ticket | ControlDbUnreachableError | TicketNotFoundError:
    """The CLI ``ticket_id`` as a ``Ticket``, or the refusal saying why it is not one.

    Two preconditions in a fixed order, because they need different remedies and one
    error collapsing both would hide which applies (#4170). REACHABILITY is a topology
    fact — a host process cannot open the container-only control-DB volume, ever — so
    it is read BEFORE any ORM touch rather than inferred from the ``OperationalError``
    the first query would otherwise raise. EXISTENCE is a state fact, answerable only
    once the database is open.
    """
    unreachable = unreachable_control_db_reason()
    if unreachable is not None:
        return control_db_unreachable_error(unreachable)
    try:
        return resolve_ticket(ref)
    except Ticket.DoesNotExist:
        return ticket_not_found_error(ref)
