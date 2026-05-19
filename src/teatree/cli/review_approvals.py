"""Approval CLI commands and review-before-approve precondition (#1056).

Kept separate from :mod:`teatree.cli.review` so the GitLab-MR review
mechanics module stays under the OOP/LOC ceiling
(``scripts/hooks/check_module_health.py``). Three concerns live here:

* :func:`identity_has_reviewed` — encodes the review-before-approve
    doctrine: an approval may only be recorded once the same identity
    has left a reviewing footprint (any note in any discussion thread)
    on the MR. Returns ``(reviewed, error)``; ``error`` is non-empty
    only when the approving identity itself cannot be resolved
    (a hard precondition failure, not "no review yet").
* :func:`approve` / :func:`unapprove` — free-function bodies the
    matching :class:`~teatree.cli.review.ReviewService` methods
    delegate to. Both route through the recorded-approval
    ``on_behalf_post_mode`` pre-gate (#960/#1013) before any GitLab
    API call.
* :func:`register` — wires the ``approve`` and ``unapprove`` typer
    commands onto the ``review`` typer app, mirroring the
    :mod:`teatree.cli.review_drafts` /
    :mod:`teatree.cli.review_on_behalf` registration pattern.

The helpers import their service-layer dependencies lazily inside
each command body so this module can be imported (by typer for
command discovery) before ``django.setup()`` has run, matching the
sibling :mod:`teatree.cli.review_on_behalf` pattern.
"""

from collections.abc import Callable
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import typer

from teatree.cli.review_on_behalf import check_on_behalf

if TYPE_CHECKING:
    from teatree.cli.review import ReviewService

_HTTP_OK_CODES = frozenset({HTTPStatus.OK, HTTPStatus.CREATED, HTTPStatus.NO_CONTENT})


def identity_has_reviewed(service: "ReviewService", encoded_repo: str, mr: int) -> tuple[bool, str]:
    """Whether the approving identity already authored a note on this MR.

    Encodes the review-before-approve doctrine: an approval may only be
    recorded once the same identity has left a reviewing footprint
    (any note in any discussion thread). Returns ``(reviewed, error)``;
    ``error`` is non-empty only when the identity itself cannot be
    resolved (a hard precondition failure, not "no review yet").
    """
    api = service._get_api()  # noqa: SLF001
    username = api.current_username()
    if not username:
        return False, "Could not resolve the approving GitLab identity (check token / `glab auth status`)."
    discussions = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}/discussions?per_page=100")
    if not isinstance(discussions, list):
        return False, ""
    for discussion in discussions:
        if not isinstance(discussion, dict):
            continue
        notes = discussion.get("notes")
        if not isinstance(notes, list):
            continue
        for note in notes:
            if not isinstance(note, dict):
                continue
            author = note.get("author")
            if isinstance(author, dict) and author.get("username") == username:
                return True, ""
    return False, ""


def approve(service: "ReviewService", repo: str, mr: int) -> tuple[str, int]:
    """Approve an MR — refuses unless the identity has already reviewed it.

    Returns (message, exit_code). The review-first precondition encodes
    the approve-on-review doctrine: an approval cannot be recorded
    without a prior reviewing footprint from the same identity.

    Gated by ``ask_before_post_on_behalf`` (#960/#1013): an approval is
    an outward post on the user's identity, so it routes through the
    same recorded-approval gate every other on-behalf method uses. Gate
    ON + no recorded :class:`OnBehalfApproval` matching
    ``(<repo>!<mr>, "approve")`` → refuse without any GitLab side
    effect; gate ON + recorded row → consume single-use and proceed.
    """
    blocked = check_on_behalf(repo, mr, "approve")
    if blocked:
        return blocked, 1
    encoded = repo.replace("/", "%2F")
    reviewed, error = identity_has_reviewed(service, encoded, mr)
    if error:
        return error, 1
    if not reviewed:
        msg = (
            f"Refusing to approve !{mr}: review before approve — no review note authored by your "
            "identity exists on this MR yet. Post a review (`t3 review post-comment` / "
            "`post-draft-note`) first, then approve."
        )
        return msg, 1
    api = service._get_api()  # noqa: SLF001
    status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/approve")
    if status in _HTTP_OK_CODES:
        return f"OK approved !{mr}", 0
    return f"Failed: HTTP {status}", 1


def unapprove(service: "ReviewService", repo: str, mr: int) -> tuple[str, int]:
    """Revoke this identity's approval on an MR. Returns (message, exit_code).

    No review-first precondition — removing an approval is the safe
    direction and must always be reachable.

    Gated by ``ask_before_post_on_behalf`` (#960/#1013): an unapproval
    is still a colleague-visible post on the user's identity, so it
    routes through the same recorded-approval gate as ``approve`` (and
    every other on-behalf method). The recorded row scopes to
    ``(<repo>!<mr>, "unapprove")``.
    """
    blocked = check_on_behalf(repo, mr, "unapprove")
    if blocked:
        return blocked, 1
    api = service._get_api()  # noqa: SLF001
    encoded = repo.replace("/", "%2F")
    status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/unapprove")
    if status in _HTTP_OK_CODES:
        return f"OK unapproved !{mr}", 0
    return f"Failed: HTTP {status}", 1


def _approve_command(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
) -> None:
    """Approve a GitLab MR — only after you have reviewed it.

    Precondition: a review note/discussion authored by your identity must
    already exist on the MR (review before approve). Gated by
    `on_behalf_post_mode` (BLOCK under `ask` / `draft_or_ask`,
    souliane/teatree#960/#1013) — record an approval via
    ``t3 review approve-on-behalf <repo>!<mr> approve --approver
    <user-id>`` to satisfy the gate without switching mode to
    `immediate`.
    """
    from teatree.cli.review import _require_token  # noqa: PLC0415

    service = _require_token()
    msg, code = service.approve(repo, mr)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


def _unapprove_command(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
) -> None:
    """Revoke your approval on a GitLab MR.

    No review precondition (revoking is the safe direction). Gated by
    `on_behalf_post_mode` (BLOCK under `ask` / `draft_or_ask`,
    souliane/teatree#960/#1013) — record an approval via
    ``t3 review approve-on-behalf <repo>!<mr> unapprove --approver
    <user-id>`` to satisfy the gate without switching mode to
    `immediate`.
    """
    from teatree.cli.review import _require_token  # noqa: PLC0415

    service = _require_token()
    msg, code = service.unapprove(repo, mr)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


_COMMANDS: tuple[tuple[str, Callable[..., Any]], ...] = (
    ("approve", _approve_command),
    ("unapprove", _unapprove_command),
)


def register(review_app: typer.Typer) -> None:
    """Register the approve/unapprove typer commands on the review app.

    Wired by :mod:`teatree.cli.review` at import-time so every command
    is part of ``t3 review`` exactly like the rest, while the OOP/LOC
    ceiling stays satisfied.
    """
    for name, fn in _COMMANDS:
        review_app.command(name=name)(fn)
