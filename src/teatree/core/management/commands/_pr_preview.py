"""PR ship-preview & metadata-validation helpers (split out of ``pr.py``).

Pure, command-class-free helpers that derive the PR title/description from the
ticket's last commit and validate them against the overlay's metadata rules.
Kept as a sibling module (same pattern as ``_ship/fsm.py``) so ``pr.py`` stays
within the module-health LOC budget and the "ship preview" concern is named
by its own file (self-documenting hierarchy).

Invariant (MR title/description divergence guard): the description's first
line is built from the *same* sanitized string as the title, so they can
never diverge by construction — a diverged title vs. description-first-line
is exactly what blocks the release-notes pipeline.
"""

from typing import TypedDict

from teatree.core.models import Ticket, Worktree
from teatree.core.overlay_loader import get_overlay
from teatree.core.review.mr_metadata import ensure_standard_body
from teatree.core.runners.ship import (
    PrTitleInputs,
    overlay_pr_labels,
    resolve_pr_title,
    sanitize_close_keywords,
    should_close_ticket,
)
from teatree.utils import git


class ShipDryRun(TypedDict):
    dry_run: bool
    repo: str
    branch: str
    title: str
    description: str
    labels: list[str]


class PrValidationError(TypedDict):
    error: str
    details: list[str]


def ship_preview(ticket: Ticket, worktree: Worktree, *, title: str = "") -> tuple[str, str, str]:
    """Return ``(repo_path, title, description)`` for the PR that will ship.

    The title is resolved through ``runners.ship.resolve_pr_title`` — the SAME
    helper ``ShipExecutor._build_pr_spec`` uses — so the preview (and the
    ``pr create`` preflight built on it) validate the title that will actually
    ship, not a title regenerated from the commit subject. An explicit
    ``--title`` (the ``title`` arg) wins, then a title pinned on
    ``extra['pr_title_override']``, then the overlay-PRODUCED title
    (``metadata.build_pr_title``, returning the subject unchanged by default),
    then the ``Resolve <issue_url>`` fallback.

    Honoring the override here is parity with ``_build_pr_spec``: ignoring it
    made the preflight apply an overlay's title grammar (e.g. a customer-MR
    requiring a GitLab issue URL) to the wrong title, false-failing a tooling
    PR whose pinned title satisfied the grammar.

    The TITLE is sanitized first, then the description's first line is built
    from that exact sanitized string, so the title and the description's first
    line can never diverge (the release-notes-pipeline divergence guard).
    """
    repo_path = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
    subject, body = git.last_commit_message(repo=repo_path)
    overlay = get_overlay()
    resolved = resolve_pr_title(
        ticket,
        ticket.extra or {},
        PrTitleInputs(branch=worktree.branch or "", subject=subject, body=body or "", title_override=title),
    )
    close_ticket = should_close_ticket(
        ticket.extra or {},
        setting_enabled=overlay.config.mr_close_ticket,
    )
    sanitized_title = sanitize_close_keywords(resolved, close_ticket=close_ticket)
    raw_body = sanitize_close_keywords(body, close_ticket=close_ticket) if body else ""
    description = f"{sanitized_title}\n\n{raw_body}" if raw_body else sanitized_title
    description = ensure_standard_body(
        description,
        required_sections=overlay.metadata.get_required_description_sections(),
        section_defaults=overlay.metadata.get_description_section_defaults(),
    )
    return repo_path, sanitized_title, description


def ship_dry_run(ticket: Ticket, worktree: Worktree, *, title: str = "") -> ShipDryRun:
    repo_path, resolved_title, description = ship_preview(ticket, worktree, title=title)
    return ShipDryRun(
        dry_run=True,
        repo=repo_path,
        branch=worktree.branch,
        title=resolved_title,
        description=description,
        labels=overlay_pr_labels(),
    )


def validate_pr_metadata(ticket: Ticket, worktree: Worktree, *, title: str = "") -> PrValidationError | None:
    _, resolved_title, description = ship_preview(ticket, worktree, title=title)
    validation = get_overlay().metadata.validate_pr(resolved_title, description)
    if validation["errors"]:
        return PrValidationError(error="PR validation failed", details=validation["errors"])
    return None
