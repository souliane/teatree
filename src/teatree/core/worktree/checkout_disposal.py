"""May THIS context delete or re-check-out the directory at a path? Proof, never inference.

Provisioning's reconcile step cleared its worktree slot on an INFERENCE: a directory
the clone had no registration for was "a partial checkout a prior attempt left
behind", so it was removed before ``git worktree add``. The registration survey runs
against the clone as THIS context reaches it, and a checkout records its admin dir as
an absolute path written by whatever context CREATED it — so a checkout created
elsewhere is unregistered here, reads as a partial leftover, and is deleted underneath
its writer. That is #3967: a container-view actor resolved a host directory, found no
registration for it, removed the tree and re-checked it out against its own gitdir,
taking a running agent's unstaged work with it.

Disposal therefore needs POSITIVE proof, of which there are exactly two kinds:

* the directory carries no ``.git`` entry of its own, so it never claimed to be a
    checkout — the one thing a single context can establish about a directory alone; or
* the clone this context is acting FOR vouches for it, by the admin entry NAME that
    survives a change of context (:func:`admin_entry_for`).

Everything else is unknown, and unknown keeps the directory. A gitdir pointer naming a
root absent from this context is the sharpest form of unknown: positive evidence of a
view mismatch, never of a dead checkout.

Occupancy is the second, independent signal — a directory another ticket is actively
working in is never disposable, whatever the filesystem says about it. It is what stops
a checkout this clone DOES vouch for from being torn down out from under a live writer.
"""

from pathlib import Path

from teatree.core.models import Worktree
from teatree.core.worktree.checkout_liveness import admin_entry_for, claims_to_be_a_checkout, context_scoped_pointer
from teatree.core.worktree.worktree_paths import paths_match


def disposal_refusal(path: Path, *, clone: Path, requesting_ticket_id: int | None = None) -> str:
    """Why this context must not remove or re-check-out *path*, or ``""`` when it may.

    The refusal is the caller's report verbatim: the operator's next action differs per
    reason — act from the context that resolves the gitdir root, or wait for the live
    writer — and neither is guessable from a bare "disposal refused".

    *clone* is the source clone as THIS context reaches it — the only registry whose
    vouching means anything here. *requesting_ticket_id* is the ticket the caller is
    provisioning FOR, excluded from the occupancy scan so a ticket's own re-provision
    is never blocked by its own liveness.
    """
    if not path.is_dir():
        return ""
    if occupant := _live_occupant(path, requesting_ticket_id):
        return (
            f"{path} is held by ticket {occupant}, which has a live session or an active task. "
            f"Refusing to remove or re-create a checkout with a live writer."
        )
    if not claims_to_be_a_checkout(path) or admin_entry_for(path, clone) is not None:
        return ""
    return _unproven_refusal(path, clone)


def _unproven_refusal(path: Path, clone: Path) -> str:
    """The refusal text for a checkout no reachable clone vouches for.

    Split on whether the checkout's own pointer resolves here, because the two are
    different operator problems: an unresolvable pointer names the foreign context to
    act from, while a resolvable one names a clone other than the one being acted for.
    """
    if (pointer := context_scoped_pointer(path)) is not None:
        return (
            f"{path} records its git admin dir at {pointer.target}, a root that does not exist in this "
            f"execution context. That is evidence of a VIEW MISMATCH, not of a dead checkout. Refusing to "
            f"remove or re-create it — act from the context that resolves {pointer.target}."
        )
    return (
        f"{path} holds a checkout that {clone} does not vouch for. Unproven is not dead: refusing to remove "
        f"or re-create it. Dispose of it from the clone whose admin registry holds it."
    )


def _live_occupant(path: Path, requesting_ticket_id: int | None) -> str:
    """The ticket reference of a FOREIGN ticket actively working in *path*, or ``""``.

    Reads the same ticket-liveness rule the reapers and ``workspace relocate`` consult
    (:meth:`~teatree.core.models.ticket.Ticket.has_active_work`), so occupancy cannot
    mean one thing here and another there.
    """
    rows = Worktree.objects.select_related("ticket")
    if requesting_ticket_id is not None:
        rows = rows.exclude(ticket_id=requesting_ticket_id)
    for row in rows:
        recorded = row.worktree_path
        if recorded and paths_match(recorded, path) and row.ticket.has_active_work():
            return row.ticket.ticket_number or str(row.ticket.pk)
    return ""


__all__ = ["disposal_refusal"]
