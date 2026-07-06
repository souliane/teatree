"""Ticket-intake concern for ``t3 teatree workspace ticket`` (#2217).

Split from :mod:`workspace` to keep the command module under the per-module
LOC cap. Holds the get-or-create + scope/start transaction, the flat
``<number>-<slug>`` branch-name builder, and the #2217 filesystem-evidence
double-dispatch guard that runs inside that transaction so a refusal rolls the
freshly-created ticket back and leaves no DB trace.
"""

import json
import logging
import re
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

import httpx
from django.db import transaction

from teatree.backends.errors import IssueNotFoundError
from teatree.backends.loader import get_code_host_for_url
from teatree.core.dev_repo import parse_repo_branch_map, resolve_repo_names
from teatree.core.management.commands import _workspace_helpers as _wh
from teatree.core.models import Ticket
from teatree.core.models.external_delivery import mark_external_delivery
from teatree.core.models.project_learning import ProjectLearning
from teatree.core.models.ticket_display import format_intake_summary
from teatree.core.resolve import _get_user_cwd
from teatree.core.runners import WorktreeProvisioner
from teatree.core.ticket_kind_classification import classify_ticket_kind, parse_kind
from teatree.core.worktree_collision import find_foreign_issue_worktrees
from teatree.core.worktree_paths import ticket_dir_for
from teatree.utils import git
from teatree.utils.run import CommandFailedError, TimeoutExpired
from teatree.utils.url_slug import project_slug_from_ref

if TYPE_CHECKING:
    from teatree.core.models.types import TicketExtra
    from teatree.core.overlay import OverlayBase

logger = logging.getLogger(__name__)


class ForeignIssueWorktreeRefusedError(Exception):
    """Rolls back the ticket transaction when the #2217 foreign-dir guard refuses.

    Raised inside :func:`build_ticket`'s ``transaction.atomic()`` so a refusal
    leaves no ticket row behind; the ``ticket`` command catches it and returns 0
    (the refusal message was already written to stderr).
    """


class InvalidTicketKindError(ValueError):
    """Raised by :func:`build_intake` when ``--kind`` is not a valid Ticket.Kind (#17)."""


@dataclass(frozen=True)
class AdoptContext:
    """An existing branch + on-disk worktree to register verbatim (#2275).

    Set when ``workspace ticket --adopt`` / ``--adopt-branch`` registers a Ticket
    against work that originated OUTSIDE the derive-``<number>-<slug>`` flow. The
    operator runs the command from inside the checkout they want to adopt, so
    ``branch``, ``worktree_path`` (the checkout's on-disk root), and ``repo`` (its
    slug) are read from git. The provisioner records ``worktree_path`` instead of
    ``git worktree add`` so no second worktree dir is created.
    """

    branch: str
    worktree_path: str
    repo: str


@dataclass(frozen=True)
class TicketIntake:
    """The ``workspace ticket`` inputs that get-or-create + scope/start a ticket."""

    issue_url: str
    variant: str
    repo_names: list[str]
    description: str
    take_over: bool
    # #33: per-repo branch map (repo -> branch). The ticket DIR is still the
    # single ``extra['branch']``; only the per-repo git branch differs, so split
    # branches provision as SIBLINGS in one dir. A repo absent from the map
    # falls back to ``extra['branch']`` in the provisioner. Empty = uniform.
    branches: dict[str, str] = field(default_factory=dict)
    adopt: "AdoptContext | None" = None
    # #17: explicit ``--kind`` (``fix``/``feature``). Blank defers to inference.
    kind: str = ""


@dataclass(frozen=True)
class RawTicketInputs:
    """The raw ``workspace ticket`` CLI flags, before repo/branch resolution."""

    issue_url: str
    repos: str
    variant: str
    description: str
    take_over: bool
    adopt: "AdoptContext | None" = None
    kind: str = ""


def resolve_adopt_context(*, adopt: bool, adopt_branch: str) -> AdoptContext | None:
    """Read the branch/worktree/repo to adopt from the current git worktree (#2275).

    Returns ``None`` when neither flag is set (not adopting). Otherwise runs from
    inside the checkout the operator wants to register: *adopt_branch* overrides
    the branch, else the currently-checked-out branch is auto-detected; the
    worktree's on-disk root and repo slug are read from git so the provisioner
    records the existing checkout verbatim rather than creating a second dir.
    """
    if not (adopt or adopt_branch):
        return None
    cwd = _get_user_cwd()
    worktree_path = git.run(repo=cwd, args=["rev-parse", "--show-toplevel"]) or cwd
    branch = adopt_branch or git.current_branch(repo=worktree_path)
    repo = git.remote_slug(repo=worktree_path) or Path(worktree_path).name
    return AdoptContext(branch=branch, worktree_path=worktree_path, repo=repo)


def adopt_preflight_refusal(
    overlay: "OverlayBase", issue_url: str, adopt_ctx: AdoptContext | None, *, allow_closed: bool
) -> str | None:
    """The full ``--adopt`` precondition check: refusal message, or ``None`` to proceed.

    Not adopting (``adopt_ctx is None``) is always fine. Otherwise the branch
    must be checked out (a detached HEAD has nothing to register), and the
    target issue/PR must not be CLOSED or nonexistent (:func:`adopt_target_refusal`).
    """
    if adopt_ctx is None:
        return None
    if not adopt_ctx.branch or adopt_ctx.branch == git.DETACHED_HEAD:
        return "  Refused: --adopt needs a checked-out branch (HEAD is detached); pass --adopt-branch <branch>."
    return adopt_target_refusal(overlay, issue_url, allow_closed=allow_closed)


def adopt_target_refusal(overlay: "OverlayBase", issue_url: str, *, allow_closed: bool) -> str | None:
    """Refusal message when ``--adopt``'s target issue/PR is CLOSED or nonexistent, else ``None``.

    Anti-recurrence guard: ``--adopt`` binds a Ticket to whatever ``issue_url``
    the operator typed, and the recorded failure was adopting a stray number
    that resolved to a CLOSED, unrelated PR. We resolve the URL's live state
    through the per-URL code host and refuse a closed or not-found target; the
    ``--adopt-closed`` override (``allow_closed``) is the explicit escape.

    Fail-open on the ambiguous cases — a URL no code host can resolve (an
    unknown tracker) or a transient forge error is NOT proof of a bad target,
    so it proceeds. The refusal fires only on a POSITIVE closed / not-found
    signal from a resolvable host.
    """
    host = get_code_host_for_url(overlay, issue_url)
    if host is None:
        return None
    try:
        data = host.get_issue(issue_url)
    except IssueNotFoundError:
        return f"  Refused: --adopt target {issue_url} does not exist (HTTP 404). Check the issue/PR URL."
    except (httpx.HTTPError, CommandFailedError, TimeoutExpired, json.JSONDecodeError, OSError) as exc:
        logger.warning("adopt-target validation could not resolve %s: %s", issue_url, exc)
        return None
    if not isinstance(data, dict) or data.get("error"):
        return f"  Refused: --adopt target {issue_url} does not resolve to a real issue/PR. Check the URL."
    if overlay.is_issue_done(data) and not allow_closed:
        return (
            f"  Refused: --adopt target {issue_url} is CLOSED. Adopting a closed issue/PR is the "
            "recorded mis-adoption failure; pass --adopt-closed to override if this is intentional."
        )
    return None


def build_intake(overlay: "OverlayBase", raw: RawTicketInputs) -> TicketIntake:
    """Resolve the raw ``workspace ticket`` CLI inputs into a :class:`TicketIntake`.

    Splits the ``--repos`` string into bare repo names and, per #33, the
    per-repo ``repo:branch`` override map — both derived from the one string
    so the CLI command body stays thin. In adopt mode (#2275) the repo set is
    the single adopted repo read from git, not the overlay/issue derivation.
    """
    if raw.kind.strip():
        try:
            parse_kind(raw.kind)
        except ValueError as exc:
            raise InvalidTicketKindError(str(exc)) from exc
    repo_names = [raw.adopt.repo] if raw.adopt else resolve_repo_names(overlay, raw.issue_url, raw.repos)
    return TicketIntake(
        issue_url=raw.issue_url,
        variant=raw.variant,
        repo_names=repo_names,
        description=raw.description,
        take_over=raw.take_over,
        branches=parse_repo_branch_map(raw.repos),
        adopt=raw.adopt,
        kind=raw.kind,
    )


def slugify(text: str, max_length: int = 40) -> str:
    """Convert text to a URL-safe slug for branch names."""
    return re.sub(r"[^a-z0-9]+", "-", text.strip().lower()).strip("-")[:max_length]


def build_branch_name(repo_names: list[str], ticket_number: str, description: str) -> str:
    """Build the flat ``<number>-<slug>`` branch name; legacy initials/repo prefix dropped (#1323)."""
    del repo_names
    slug = slugify(description) if description else "ticket"
    return f"{ticket_number}-{slug}"


def locked_get_or_create_ticket(
    issue_url: str,
    variant: str,
    repo_names: list[str],
    *,
    kind: Ticket.Kind = Ticket.Kind.FEATURE,
) -> Ticket:
    """Get-or-create the ticket and lock it for the provisioning RMW.

    #800 N3: ``get_or_create`` does not lock the row; the subsequent
    ``scope()`` + ``repos`` + ``extra`` + full ``save()`` is a
    read-modify-write that a concurrent provisioner for the same
    ``issue_url`` would lost-update. On an existing row we re-fetch it
    ``select_for_update``-locked (the ``ensure_session()`` pattern,
    ``ticket.py``); a freshly-created row is already exclusive to this
    transaction. Caller must be inside ``transaction.atomic()``.

    ``kind`` (#17) is stamped only on a freshly-created row (``defaults``), so
    re-running ``workspace ticket`` never reclassifies an existing ticket.
    """
    ticket, created = Ticket.objects.get_or_create(
        issue_url=issue_url,
        defaults={"variant": variant, "repos": repo_names, "kind": kind},
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
        # #17: classify BEFORE the get-or-create so the FIX/FEATURE kind is stamped
        # in the row's ``defaults`` (create-only, never reclassifying an existing
        # ticket). The title feeds the inference; an explicit ``--kind`` wins.
        description = intake.description or overlay.get_issue_title(intake.issue_url)
        ticket = locked_get_or_create_ticket(
            intake.issue_url,
            intake.variant,
            intake.repo_names,
            kind=classify_ticket_kind(title=description, explicit=intake.kind),
        )

        # Refuse a silent rebind when --variant disagrees with the existing ticket's variant (#1306).
        _wh.reject_variant_mismatch(write, ticket, intake.variant)

        if ticket.state == Ticket.State.NOT_STARTED:
            ticket.scope(issue_url=intake.issue_url, variant=intake.variant or None, repos=intake.repo_names)

        ticket.repos = list(dict.fromkeys((ticket.repos or []) + intake.repo_names))

        extra = cast("TicketExtra", ticket.extra or {})
        if intake.adopt:
            # Adopt (#2275): register the ticket against the EXISTING branch the
            # operator handed us, not a derived ``<number>-<slug>``, and record
            # the on-disk worktree path so the provisioner reuses it (no second
            # dir). ``adopt`` maps repo -> existing worktree_path.
            extra["branch"] = intake.adopt.branch
            adopt_map = dict(extra.get("adopt") or {})
            adopt_map[intake.adopt.repo] = intake.adopt.worktree_path
            extra["adopt"] = adopt_map
        elif not extra.get("branch"):
            extra["branch"] = build_branch_name(intake.repo_names, ticket.ticket_number, description)
        if description and not extra.get("description"):
            extra["description"] = description
        # #33: merge any per-repo branch overrides so a split-branch ticket
        # provisions each repo on its own branch while sharing one ticket dir.
        # Merge (not replace) so re-running ``ticket`` to add a repo's branch
        # keeps the branches already recorded for sibling repos.
        if intake.branches:
            merged = dict(extra.get("branches") or {})
            merged.update(intake.branches)
            extra["branches"] = merged
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
        # Adopt (#2275) is an explicit "use this existing checkout" intent, so it
        # opts out of the foreign-dir guard exactly like --take-over.
        if not intake.take_over and not intake.adopt:
            _refuse_on_foreign_issue_worktree(
                write, ticket, workspace_root, ticket_dir_for(workspace_root, extra["branch"])
            )

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


def _project_learnings_for_ticket(ticket: Ticket) -> str:
    """Durable per-repo learnings (#2892) for *ticket*'s repo, or "" when none recorded."""
    slug = project_slug_from_ref(ticket.issue_url)
    return ProjectLearning.objects.content_for_slug(slug) if slug else ""


def finalize_ticket_provision(
    write_out: Callable[[str], None],
    write_err: Callable[[str], None],
    ticket: Ticket,
    adopt_ctx: AdoptContext | None,
    workspace_root: Path,
) -> int:
    """Provision the ticket's worktrees, discard an unattested failure, print the summary.

    The second half of ``workspace ticket``, split from the command module to
    keep it under the per-module LOC cap. Returns the ticket pk on success (or a
    soft-failure warning), or 0 when an unrecoverable provision aborted and the
    unattested ticket was discarded.
    """
    branch = cast("TicketExtra", ticket.extra)["branch"]
    # In adopt mode the checkout lives where the operator ran the command, not
    # under the worktree root — surface that path in the summary.
    ticket_dir = Path(adopt_ctx.worktree_path).parent if adopt_ctx else ticket_dir_for(workspace_root, branch)

    # Run the provisioner synchronously so the CLI gives immediate feedback; the
    # worker that ``start()`` enqueued is idempotent and no-ops when it finds the
    # worktrees already in place. Single source of truth: the runner.
    result = WorktreeProvisioner(ticket).run()
    if not result.ok and not ticket.worktrees.exists():  # ty: ignore[unresolved-attribute]
        write_err(f"  Provisioning failed: {result.detail}")
        # #748: only discard the ticket if it carries NO phase attestation.
        # ``get_or_create`` may have resolved an existing loop/coordinator-built
        # ticket whose sessions hold genuinely-completed-work phase records;
        # ``Session.ticket`` is ``on_delete=CASCADE``, so ``ticket.delete()``
        # here would cascade-reap that attestation. A failed provision must never
        # destroy attested work — leave the ticket + sessions intact.
        visited, _ = ticket.aggregate_phase_records()
        if not visited:
            ticket.delete()
            with suppress(OSError):
                ticket_dir.rmdir()
        return 0
    if not result.ok:
        write_err(f"  WARNING: {result.detail}")
    learnings = _project_learnings_for_ticket(ticket)
    write_out(format_intake_summary(ticket, str(ticket_dir), branch, project_learnings=learnings))
    return int(ticket.pk)
