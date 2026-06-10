"""Ticket-intake concern for ``t3 teatree workspace ticket`` (#2217).

Split from :mod:`workspace` to keep the command module under the per-module
LOC cap. Holds the get-or-create + scope/start transaction, the flat
``<number>-<slug>`` branch-name builder, and the #2217 filesystem-evidence
double-dispatch guard that runs inside that transaction so a refusal rolls the
freshly-created ticket back and leaves no DB trace.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, cast

from django.db import transaction

from teatree.core.management.commands import _workspace_helpers as _wh
from teatree.core.models import Ticket
from teatree.core.models.external_delivery import mark_external_delivery
from teatree.core.worktree_collision import find_foreign_issue_worktrees

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra
    from teatree.core.overlay import OverlayBase


class ForeignIssueWorktreeRefusedError(Exception):
    """Rolls back the ticket transaction when the #2217 foreign-dir guard refuses.

    Raised inside :func:`build_ticket`'s ``transaction.atomic()`` so a refusal
    leaves no ticket row behind; the ``ticket`` command catches it and returns 0
    (the refusal message was already written to stderr).
    """


@dataclass(frozen=True)
class TicketIntake:
    """The ``workspace ticket`` inputs that get-or-create + scope/start a ticket."""

    issue_url: str
    variant: str
    repo_names: list[str]
    description: str
    take_over: bool


def slugify(text: str, max_length: int = 40) -> str:
    """Convert text to a URL-safe slug for branch names."""
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")[:max_length]


def build_branch_name(repo_names: list[str], ticket_number: str, description: str) -> str:
    """Build the flat ``<number>-<slug>`` branch name; legacy initials/repo prefix dropped (#1323)."""
    del repo_names
    slug = slugify(description) if description else "ticket"
    return f"{ticket_number}-{slug}"


def locked_get_or_create_ticket(issue_url: str, variant: str, repo_names: list[str]) -> Ticket:
    """Get-or-create the ticket and lock it for the provisioning RMW.

    #800 N3: ``get_or_create`` does not lock the row; the subsequent
    ``scope()`` + ``repos`` + ``extra`` + full ``save()`` is a
    read-modify-write that a concurrent provisioner for the same
    ``issue_url`` would lost-update. On an existing row we re-fetch it
    ``select_for_update``-locked (the ``ensure_session()`` pattern,
    ``ticket.py``); a freshly-created row is already exclusive to this
    transaction. Caller must be inside ``transaction.atomic()``.
    """
    ticket, created = Ticket.objects.get_or_create(
        issue_url=issue_url,
        defaults={"variant": variant, "repos": repo_names},
    )
    if created:
        return ticket
    return Ticket.objects.select_for_update().get(pk=ticket.pk)


def build_ticket(
    write: Callable[[str], None],
    overlay: "OverlayBase",
    intake: TicketIntake,
    workspace_root: Path,
) -> Ticket:
    """Get-or-create + scope/start the ticket inside one transaction, guarding the seam (#2217).

    Raises :class:`ForeignIssueWorktreeRefusedError` when the foreign-dir guard
    refuses; the ``raise`` inside ``transaction.atomic()`` rolls back any
    freshly-created ticket so a refusal leaves zero DB trace.
    """
    with transaction.atomic():
        ticket = locked_get_or_create_ticket(intake.issue_url, intake.variant, intake.repo_names)

        # Refuse a silent rebind when --variant disagrees with the existing ticket's variant (#1306).
        _wh.reject_variant_mismatch(write, ticket, intake.variant)

        if ticket.state == Ticket.State.NOT_STARTED:
            ticket.scope(issue_url=intake.issue_url, variant=intake.variant or None, repos=intake.repo_names)

        ticket.repos = list(dict.fromkeys((ticket.repos or []) + intake.repo_names))

        description = intake.description or overlay.get_issue_title(intake.issue_url)

        extra = cast("TicketExtra", ticket.extra or {})
        if not extra.get("branch"):
            extra["branch"] = build_branch_name(intake.repo_names, ticket.ticket_number, description)
        if description and not extra.get("description"):
            extra["description"] = description
        ticket.extra = extra
        ticket.save()

        # #2217: filesystem-evidence double-dispatch guard, inside the transaction
        # so a refusal ROLLS BACK any freshly-created ticket — we leave no
        # stranded row for a unit someone else is already provisioning. The DB
        # lease (#2104) cannot see a race that left no ticket, but the `N-*`
        # worktree dir on disk is unambiguous. ticket_dir is the ticket's OWN dir,
        # so re-provisioning it is idempotent; --take-over opts out. Runs before
        # the on-disk worktree and the delivery-ownership claim so neither side
        # effect survives a refusal.
        if not intake.take_over:
            _refuse_on_foreign_issue_worktree(write, ticket, workspace_root, workspace_root / extra["branch"])

        # #2104: this CLI IS the hand-dispatched external-delivery entry — a
        # directly-implementing delivery agent (per /teatree-batch) runs it, the
        # loop's own FSM never does. Claim delivery ownership so the loop's
        # scheduling chokepoints (execute_provision before schedule_planning; the
        # pr_sweep review-arm) skip the auto-planner / duplicate review-arm the
        # external owner will never consume.
        mark_external_delivery(ticket)

        if ticket.state == Ticket.State.SCOPED:
            ticket.start()
            ticket.save()

        # #748: every entry point converges on a durable session so the shipping
        # gate has a phase-attestation home regardless of which path created the
        # ticket.
        ticket.ensure_session()
    return ticket


def _refuse_on_foreign_issue_worktree(
    write: Callable[[str], None], ticket: Ticket, workspace_root: Path, ticket_dir: Path
) -> None:
    """Raise :class:`ForeignIssueWorktreeRefusedError` on a foreign-dir collision (#2217), else proceed.

    Globs the workspace for a ``<issue>-*`` worktree dir at a path other than the
    ticket's own, naming any collision so the operator can investigate.
    """
    foreign = find_foreign_issue_worktrees(
        ticket.ticket_number,
        own_path=ticket_dir,
        workspace_dir=workspace_root,
    )
    if not foreign:
        return
    paths = ", ".join(str(p) for p in foreign)
    write(
        f"  Refused: issue #{ticket.ticket_number} already has a worktree at {paths}; "
        "someone may already be working it. Re-run with --take-over to proceed."
    )
    raise ForeignIssueWorktreeRefusedError
