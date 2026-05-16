"""PR ship-preview & metadata-validation helpers (split out of ``pr.py``).

Pure, command-class-free helpers that derive the PR title/description from the
ticket's last commit and validate them against the overlay's metadata rules.
Kept as a sibling module (same pattern as ``_ship_fsm.py``) so ``pr.py`` stays
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
from teatree.core.runners.ship import overlay_pr_labels, sanitize_close_keywords
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


def ship_preview(ticket: Ticket, worktree: Worktree) -> tuple[str, str, str]:
    """Return ``(repo_path, title, description)`` previewed from the last commit.

    Sanitizes the TITLE first, then builds the description's first line from
    that exact sanitized string. Applying close-keyword sanitization only to
    the description (the old behaviour) silently diverged it from the title
    whenever the title carried a close-keyword (e.g. the ``Resolve <url>``
    fallback, or a ``fix: resolve X`` subject) — the title/description
    divergence class. Reusing the sanitized title makes the first line ==
    title by construction.
    """
    repo_path = (worktree.extra or {}).get("worktree_path", "") or worktree.repo_path
    subject, body = git.last_commit_message(repo=repo_path)
    close_ticket = get_overlay().config.mr_close_ticket
    title = sanitize_close_keywords(subject or f"Resolve {ticket.issue_url}", close_ticket=close_ticket)
    raw_body = sanitize_close_keywords(body, close_ticket=close_ticket) if body else ""
    description = f"{title}\n\n{raw_body}" if raw_body else title
    return repo_path, title, description


def ship_dry_run(ticket: Ticket, worktree: Worktree) -> ShipDryRun:
    repo_path, title, description = ship_preview(ticket, worktree)
    return ShipDryRun(
        dry_run=True,
        repo=repo_path,
        branch=worktree.branch,
        title=title,
        description=description,
        labels=overlay_pr_labels(),
    )


def validate_pr_metadata(ticket: Ticket, worktree: Worktree) -> PrValidationError | None:
    _, title, description = ship_preview(ticket, worktree)
    validation = get_overlay().metadata.validate_pr(title, description)
    if validation["errors"]:
        return PrValidationError(error="PR validation failed", details=validation["errors"])
    return None
