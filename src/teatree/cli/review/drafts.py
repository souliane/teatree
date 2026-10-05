"""Draft-note management CLI commands.

Kept separate from :mod:`teatree.cli.review` so the GitLab-MR review
mechanics module stays under the OOP/LOC ceiling
(``scripts/hooks/check_module_health.py``). :func:`register` wires the ``delete-draft-note``,
    ``delete-discussion``, ``delete-issue-note``, ``list-draft-notes``,
    ``publish-draft-notes``, ``resolve-discussion``, and ``update-note``
    typer commands onto the ``review`` typer app. The issue-note variant is
    the sanctioned path the ``block-raw-review-post`` hook (#1164) leaves no
    other way to take. These are the draft/note-management cluster:
    lifecycle operations on individual notes (publish, delete, list,
    edit, resolve), distinct from posting a comment.

The helpers import their service-layer dependencies lazily inside
each command body so this module can be imported (by typer for
command discovery) before ``django.setup()`` has run, matching the
sibling :mod:`teatree.cli.review.on_behalf` pattern.

Every command passes its own ``repo`` argument to ``_require_token``:
that slug is the overlay-resolution context both the credential and the
API base URL are read through, so an install carrying several overlays
resolves them from the overlay OWNING the target repo instead of failing
the ambient lookup with ``Multiple overlays found``
(souliane/teatree#1814 class).
"""

from collections.abc import Callable
from typing import Any

import typer


def _delete_draft_note(
    repo: str = typer.Argument(help="GitLab project path"),
    mr: int = typer.Argument(help="Merge request IID"),
    note_id: int = typer.Argument(help="Draft note ID to delete"),
) -> None:
    """Delete a draft note from a GitLab MR."""
    from teatree.cli.review.commands import _require_token  # noqa: PLC0415 — deferred: breaks drafts ↔ commands cycle

    service = _require_token(repo)
    msg, code = service.delete_draft_note(repo, mr, note_id)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


def _delete_discussion(
    repo: str = typer.Argument(help="GitLab project path"),
    mr: int = typer.Argument(help="Merge request IID"),
    note_id: int = typer.Argument(help="Published note ID to delete"),
) -> None:
    """Delete a *published* note (discussion) from a GitLab MR.

    Use to clean up a published general comment that should have
    been inline, or any other published note that needs removal.
    Distinct from `delete-draft-note`, which removes a user's own
    pre-publication draft. Respects the posture
    pre-gate (souliane/teatree#960).
    """
    from teatree.cli.review.commands import _require_token  # noqa: PLC0415 — deferred: breaks drafts ↔ commands cycle

    service = _require_token(repo)
    msg, code = service.delete_discussion(repo, mr, note_id)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


def _delete_issue_note(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    issue_iid: int = typer.Argument(help="Issue / work-item IID"),
    note_id: int = typer.Argument(help="Published note ID to delete"),
) -> None:
    """Delete a *published* note from a GitLab ISSUE / work-item.

    The issue/work-item twin of `delete-discussion` (which removes an MR
    note). Use to clean up a published note on an issue/work-item under
    the user's identity. This is the sanctioned path: a raw
    `glab api --method DELETE projects/.../issues/<iid>/notes/<id>` is
    denied by the `block-raw-review-post` hook (souliane/teatree#1164),
    which has no bypass — only this command routes through the on-behalf
    pre-gate the raw write skips. Respects the posture
    pre-gate (#960), scoped to `<repo>#<issue>` (record an approval via
    `t3 review approve-on-behalf <repo>#<issue> delete_issue_note
    --approver <user-id>`).
    """
    from teatree.cli.review.commands import _require_token  # noqa: PLC0415 — deferred: breaks drafts ↔ commands cycle

    service = _require_token(repo)
    msg, code = service.delete_issue_note(repo, issue_iid, note_id)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


def _publish_draft_notes(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
) -> None:
    """Publish all draft notes on a GitLab MR (bulk submit)."""
    from teatree.cli.review.commands import _require_token  # noqa: PLC0415 — deferred: breaks drafts ↔ commands cycle

    service = _require_token(repo)
    msg, code = service.publish_draft_notes(repo, mr)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


def _list_draft_notes(
    repo: str = typer.Argument(help="GitLab project path"),
    mr: int = typer.Argument(help="Merge request IID"),
) -> None:
    """List draft notes on a GitLab MR."""
    from teatree.cli.review.commands import _require_token  # noqa: PLC0415 — deferred: breaks drafts ↔ commands cycle

    service = _require_token(repo)
    msg, _code = service.list_draft_notes(repo, mr)
    typer.echo(msg)


def _update_note(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
    note_id: int = typer.Argument(help="Note ID (draft or published)"),
    body: str = typer.Argument(help="New comment body (markdown)"),
) -> None:
    """Update a note on a GitLab MR — auto-detects draft vs published."""
    from teatree.cli.review.commands import _require_token  # noqa: PLC0415 — deferred: breaks drafts ↔ commands cycle

    service = _require_token(repo)
    msg, code = service.update_note(repo, mr, note_id, body)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


def _resolve_discussion(
    repo: str = typer.Argument(help="GitLab project path"),
    mr: int = typer.Argument(help="Merge request IID"),
    discussion_id: str = typer.Argument(help="Discussion (thread) ID"),
    *,
    resolved: bool = typer.Option(True, "--resolved/--no-resolved", help="Mark resolved (default) or re-open."),
) -> None:
    """Mark a GitLab MR discussion thread resolved or unresolved."""
    from teatree.cli.review.commands import _require_token  # noqa: PLC0415 — deferred: breaks drafts ↔ commands cycle

    service = _require_token(repo)
    msg, code = service.resolve_discussion(repo, mr, discussion_id, resolved=resolved)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


_COMMANDS: tuple[tuple[str, Callable[..., Any]], ...] = (
    ("delete-draft-note", _delete_draft_note),
    ("delete-discussion", _delete_discussion),
    ("delete-issue-note", _delete_issue_note),
    ("publish-draft-notes", _publish_draft_notes),
    ("list-draft-notes", _list_draft_notes),
    ("update-note", _update_note),
    ("resolve-discussion", _resolve_discussion),
)


def register(review_app: typer.Typer) -> None:
    """Register the draft/note-management typer commands on the review app.

    Wired by :mod:`teatree.cli.review` at import-time so every command
    is part of ``t3 review`` exactly like the rest, while the OOP/LOC
    ceiling stays satisfied.
    """
    for name, fn in _COMMANDS:
        review_app.command(name=name)(fn)
