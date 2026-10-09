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
mandatory-E2E (:func:`evaluate_e2e_mandatory`) refuses a
customer-display-impacting change lacking green E2E evidence (or a single-use
bypass) at the reviewed tree, and refuses as DID NOT RUN when the changed-file
diff cannot be read; a no-op for an out-of-FSM CLEAR, which has no ticket
evidence to bind to.
"""

from typing import TYPE_CHECKING, cast

from teatree.core.gates.e2e_mandatory_gate import evaluate_e2e_mandatory
from teatree.core.management.commands._clear_branch_currency import check_clear_branch_currency
from teatree.core.management.commands._clear_migration_fork import check_clear_migration_fork
from teatree.core.modelkit.gate_verdict import EvidenceUnavailableError

if TYPE_CHECKING:
    from teatree.core.models import Ticket
    from teatree.core.models.types import TicketExtra


def resolve_clear_changed_files(ticket: "Ticket") -> list[str]:
    """Resolve the INVOKING worktree's diff for the #1967 CLEAR-side E2E gate.

    Lives in the command layer (not the domain gate) so the integration-layer
    git-diff helper is reached from a layer allowed to depend on it. Shares the
    canonical :func:`resolve_ship_worktree` (#776) so the CLEAR side classifies
    the same tree the ship side does — the checkout the CLEAR acts on, recorded on
    ``extra['ship_invoking_path']`` (its branch the fallback) — not the ticket's
    earliest (often already-merged) worktree row a reused multi-workstream ticket
    carries.
    Returns the changed-file list against the resolved diff base. Raises when the
    diff cannot be read — an ambiguous worktree, no worktree (never the cwd), a
    failing git — so the gate refuses rather than classifying an empty list.
    """
    from teatree import visual_qa  # noqa: PLC0415 — deferred: keeps command import light
    from teatree.core.runners.ship import (  # noqa: PLC0415 — deferred: keeps command import light
        ShipWorktreeAmbiguousError,
        resolve_ship_worktree,
    )

    extra = cast("TicketExtra", ticket.extra or {})
    try:
        worktree = resolve_ship_worktree(ticket, extra)
    except ShipWorktreeAmbiguousError as exc:
        msg = f"the CLEAR's worktree is ambiguous: {exc}"
        raise EvidenceUnavailableError(msg) from exc
    if worktree is None:
        msg = f"ticket {ticket.pk} records no worktree, so the reviewed tree's changed files cannot be read"
        raise EvidenceUnavailableError(msg)
    return visual_qa.changed_files(repo=worktree.worktree_path or worktree.repo_path)


def clear_preflight_refusal(reviewed_sha: str, ticket: "Ticket | None") -> str | None:
    """First refusal from the ordered pre-``MergeClear.issue`` gate chain, else ``None``."""
    currency_error = check_clear_branch_currency(reviewed_sha, ticket)
    if currency_error is not None:
        return currency_error
    migration_fork_error = check_clear_migration_fork(reviewed_sha, ticket)
    if migration_fork_error is not None:
        return migration_fork_error
    if ticket is None:
        return None
    return (
        evaluate_e2e_mandatory(
            ticket, read_head=lambda: reviewed_sha, read_diff=lambda: resolve_clear_changed_files(ticket)
        )
        or None
    )
