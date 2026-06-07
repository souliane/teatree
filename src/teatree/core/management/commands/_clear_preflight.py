"""The ordered pre-``MergeClear.issue`` gate chain for ``ticket clear``.

Owns the command-layer pre-issuance gating so :mod:`ticket` stays a thin
dispatcher. Each gate runs BEFORE issuance, so a CLEAR — if issued —
already points at a current, conflict-free, end-to-end-mergeable SHA.

#940 branch-currency (:mod:`_clear_branch_currency`) refuses when
``reviewed_sha`` trails the target AND merging would textually conflict
(behind-but-clean is allowed). #995 migration-fork
(:mod:`_clear_migration_fork`) refuses when the merged tree would fork the
migration graph (two migrations off one parent) — each branch is linear in
isolation, so only the merged tree exposes the leaf collision the post-merge
``migrate`` rejects with 'Conflicting migrations detected'. #1967
mandatory-E2E (:func:`check_clear_e2e_mandatory`) refuses a
customer-display-impacting change lacking green E2E evidence (or a single-use
bypass) at the reviewed tree; a no-op for an out-of-FSM CLEAR.
"""

from typing import TYPE_CHECKING, cast

from teatree.core.gates.e2e_mandatory_gate import check_clear_e2e_mandatory
from teatree.core.management.commands._clear_branch_currency import check_clear_branch_currency
from teatree.core.management.commands._clear_migration_fork import check_clear_migration_fork

if TYPE_CHECKING:
    from teatree.core.models import Ticket
    from teatree.core.models.types import TicketExtra


def resolve_clear_changed_files(ticket: "Ticket | None") -> list[str]:
    """Resolve the INVOKING worktree's diff for the #1967 CLEAR-side E2E gate.

    Lives in the command layer (not the domain gate) so the integration-layer
    git-diff helper is reached from a layer allowed to depend on it. Shares the
    canonical :func:`resolve_ship_worktree` (#776) so the CLEAR side classifies
    the same tree the ship side does — the branch the CLEAR acts on, recorded on
    ``extra['ship_invoking_branch']`` — not the ticket's earliest (often
    already-merged) worktree row a reused multi-workstream ticket carries.
    Returns the ``origin/main...HEAD`` changed-file list, or an empty list when
    no worktree / no resolvable diff (the gate treats an empty diff as
    fail-closed impacting for a customer-facing overlay).
    """
    if ticket is None:
        return []

    from teatree import visual_qa  # noqa: PLC0415
    from teatree.core.runners.ship import resolve_ship_worktree  # noqa: PLC0415
    from teatree.utils.run import CommandFailedError  # noqa: PLC0415

    extra = cast("TicketExtra", ticket.extra or {})
    worktree = resolve_ship_worktree(ticket, extra)
    repo_path = (worktree.worktree_path or worktree.repo_path) if worktree else "."
    try:
        return visual_qa.changed_files(repo=repo_path)
    except (CommandFailedError, RuntimeError, ValueError):
        return []


def clear_preflight_refusal(reviewed_sha: str, ticket: "Ticket | None") -> str | None:
    """First refusal from the ordered pre-``MergeClear.issue`` gate chain, else ``None``."""
    currency_error = check_clear_branch_currency(reviewed_sha, ticket)
    if currency_error is not None:
        return currency_error
    migration_fork_error = check_clear_migration_fork(reviewed_sha, ticket)
    if migration_fork_error is not None:
        return migration_fork_error
    return check_clear_e2e_mandatory(ticket, reviewed_sha, resolve_clear_changed_files(ticket)) or None
