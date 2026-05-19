"""Typer command bindings for the review CLI.

Extracted from :mod:`teatree.cli.review` to keep that file under the
module-health LOC budget after the merge with main reintroduced the
``on_behalf_post_mode`` doctrine prose. The commands themselves are
thin shims around :class:`teatree.cli.review.ReviewService`; the
service class owns the gating + ledger logic.
"""

import typer

from teatree.cli.review import ReviewService, review_app


def _require_token() -> ReviewService:
    # Bootstrap Django (idempotent) before the on-behalf pre-gate (#960)
    # touches the ORM. CLI module stays Django-free at import time so
    # typer can render --help / discover commands; mirrors cli/loop.py.
    # See souliane/teatree#1003.
    import os  # noqa: PLC0415

    import django  # noqa: PLC0415

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "teatree.settings")
    django.setup()

    token = ReviewService.get_gitlab_token()
    if not token:
        typer.echo("No GitLab token found. Run: glab auth login")
        raise typer.Exit(code=1)
    return ReviewService(token)


@review_app.command(name="post-draft-note")
def post_draft_note(  # noqa: PLR0913 — typer command: every param is a CLI flag mapped 1:1 to the public `review post-draft-note` surface (repo/mr/note/file/line/general). The `--general` flag is load-bearing — it closes the #72 silent-degradation foot-gun by making the inline-vs-general decision explicit. The arg list IS the CLI contract, not an internal design smell (same rationale as ticket.clear / db.refresh / pr.create).
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
    note: str = typer.Argument(help="Comment text (markdown)"),
    file: str = typer.Option(
        "",
        help="File path for inline comment — REQUIRED unless --general is passed.",
    ),
    line: int | None = typer.Option(
        None,
        help="Line number in the new file (must be an added line) — REQUIRED unless --general is passed.",
    ),
    *,
    general: bool = typer.Option(
        False,
        "--general",
        help=(
            "Post a general (MR-wide) note instead of an inline one. Mutually exclusive "
            "with --file/--line. Without this flag, --file AND --line are both required "
            "— omitting either is refused upfront so a missed-flag invocation can no "
            "longer silently degrade an intended-inline draft into a general note "
            "(souliane/teatree#72)."
        ),
    ),
) -> None:
    """Post a draft note on a GitLab MR (inline or general).

    The inline-vs-general decision is explicit: pass ``--general`` for an
    MR-wide note, or pass both ``--file`` and ``--line`` for an inline
    draft. Pre-#72 the default silently degraded a missing flag pair into
    a general note — observed in !6220 where 4 of 5 cold-review drafts
    intended as inline became general. The validator
    :func:`teatree.cli.review_drafts.validate_inline_or_general` refuses
    both half-specified-inline and contradictory invocations before any
    GitLab API call is attempted.
    """
    from teatree.cli.review_drafts import validate_inline_or_general  # noqa: PLC0415

    service = _require_token()
    validate_inline_or_general(file=file, line=line, general=general)
    msg, code = service.post_draft_note(repo, mr, note, file=file, line=line or 0)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


@review_app.command(name="post-comment")
def post_comment(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
    note: str = typer.Argument(help="Comment text (markdown)"),
    file: str = typer.Option("", help="File path for inline comment (omit for general note)"),
    line: int = typer.Option(0, help="Line number in the new file (must be an added line)"),
) -> None:
    """Post an immediate (non-draft) comment on a GitLab MR.

    Useful when `post-draft-note` fails to anchor inline because the file's
    diff is collapsed (large files). This bypasses the draft workflow and
    posts straight to a discussion, where GitLab's anchoring works.
    """
    service = _require_token()
    msg, code = service.post_comment(repo, mr, note, file=file, line=line)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


@review_app.command(name="reply-to-discussion")
def reply_to_discussion(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
    discussion_id: str = typer.Argument(help="Discussion (thread) ID"),
    body: str = typer.Argument(help="Reply body (markdown)"),
) -> None:
    """Reply to a GitLab MR discussion thread (immediate, not draft)."""
    service = _require_token()
    msg, code = service.reply_to_discussion(repo, mr, discussion_id, body)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


@review_app.command(name="approve")
def approve(
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
    service = _require_token()
    msg, code = service.approve(repo, mr)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


@review_app.command(name="unapprove")
def unapprove(
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
    service = _require_token()
    msg, code = service.unapprove(repo, mr)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


__all__ = ["approve", "post_comment", "post_draft_note", "reply_to_discussion", "unapprove"]
