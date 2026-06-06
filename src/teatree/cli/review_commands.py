"""Typer command bindings for the review CLI.

Extracted from :mod:`teatree.cli.review` to keep that file under the
module-health LOC budget after the merge with main reintroduced the
``on_behalf_post_mode`` doctrine prose. The commands themselves are
thin shims around :class:`teatree.cli.review.ReviewService`; the
service class owns the gating + ledger logic.
"""

from typing import TYPE_CHECKING

import typer

from teatree.cli.review import ReviewService, review_app
from teatree.utils.django_bootstrap import ensure_django

if TYPE_CHECKING:
    from teatree.cli.review_evidence_gate import FindingEvidence


def _require_token() -> ReviewService:
    # Bootstrap Django (idempotent) before the on-behalf pre-gate (#960)
    # touches the ORM. CLI module stays Django-free at import time so
    # typer can render --help / discover commands; mirrors cli/loop.py.
    # See souliane/teatree#1003.
    ensure_django()

    token = ReviewService.get_gitlab_token()
    if not token:
        typer.echo("No GitLab token found. Run: glab auth login")
        raise typer.Exit(code=1)
    return ReviewService(token)


_EVIDENCE_JSON_HELP = (
    "Structured-evidence record (JSON) for a 'missing/wrong/broken' "
    "finding (souliane/teatree#1280). Required when the note asserts something "
    "is missing/wrong/broken/stale or does not exist. JSON keys: "
    "master_check_paths (list[str]), ticket_dep_refs (list[str]), "
    "helper_indirection_paths (list[str]), recent_merge_sweep_query (str), "
    "confidence ('verified'|'speculative'). Schema: "
    "teatree.cli.review_evidence_gate.FindingEvidence."
)


_ALLOW_LONG_REVIEW_HELP = (
    "Escape the colleague-MR review-shape cap (souliane/teatree#1114) for ONE "
    "post — the documented over-deny escape (#126), consistent with the sibling "
    "--quote-ok / --allow-banned-term overrides. Use only when a long-form review "
    "on a colleague's MR is genuinely authorized; the cap still fires by default."
)

_ALLOW_TODO_BLOCKER_HELP = (
    "Escape the TODO-anchor blocker gate (souliane/teatree#1186) for ONE post — "
    "the documented over-deny escape (#126). Use only when a blocker anchored on an "
    "author-marked TODO/FIXME genuinely must be addressed in THIS MR; the gate still "
    "refuses by default."
)


def _parse_evidence(raw: str) -> "FindingEvidence | None":
    """Build a :class:`FindingEvidence` from a CLI JSON string, or ``None`` when omitted."""
    from teatree.cli.review_evidence_gate import FindingEvidence  # noqa: PLC0415

    if not raw:
        return None
    try:
        return FindingEvidence.from_json(raw)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=1) from e


@review_app.command(name="post-draft-note")
def post_draft_note(  # noqa: PLR0913 — typer command: every param is a CLI flag mapped 1:1 to the public `review post-draft-note` surface (repo/mr/note/file/line/general/evidence-json + the #126 gate escapes). The `--general` flag is load-bearing — it closes the #72 silent-degradation foot-gun by making the inline-vs-general decision explicit. `--evidence-json` is load-bearing — it's the #1280 structured-evidence CLI plumbing.
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
    evidence_json: str = typer.Option("", "--evidence-json", help=_EVIDENCE_JSON_HELP),
    allow_long_review: bool = typer.Option(False, "--allow-long-review", help=_ALLOW_LONG_REVIEW_HELP),
    allow_todo_blocker: bool = typer.Option(False, "--allow-todo-blocker", help=_ALLOW_TODO_BLOCKER_HELP),
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
    import sys  # noqa: PLC0415

    from teatree.cli.review_drafts import validate_inline_or_general  # noqa: PLC0415

    sys.stderr.write(
        "DeprecationWarning: `t3 review post-draft-note` is deprecated (#1207). "
        "`t3 review post-comment` now defaults to creating a draft — use it instead. "
        "This subcommand routes through the same draft path and will be removed in a "
        "follow-up.\n"
    )
    service = _require_token()
    validate_inline_or_general(file=file, line=line, general=general)
    evidence = _parse_evidence(evidence_json)
    msg, code = service.post_draft_note(
        repo,
        mr,
        note,
        file=file,
        line=line or 0,
        evidence=evidence,
        allow_long_review=allow_long_review,
        allow_todo_blocker=allow_todo_blocker,
    )
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


@review_app.command(name="post-comment")
def post_comment(  # noqa: PLR0913 — typer command: every param is a CLI flag mapped 1:1 to the public ``review post-comment`` surface (repo/mr/note/file/line/live/evidence-json). ``--live`` is load-bearing — its absence is the safe-by-default draft path (#1207). ``--evidence-json`` is load-bearing — it's the #1280 structured-evidence CLI plumbing.
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
    note: str = typer.Argument(help="Comment text (markdown)"),
    file: str = typer.Option("", help="File path for inline comment (omit for general note)"),
    line: int = typer.Option(0, help="Line number in the new file (must be an added line)"),
    *,
    live: bool = typer.Option(
        False,
        "--live",
        help=(
            "Publish a colleague-visible comment directly instead of creating a draft. "
            "Requires a single-use Slack-recorded approval token minted via "
            "`t3 review approve-live-post <mr-url> --slack-ts <ts>` (#1207). The default "
            "(no flag) creates a DRAFT and DMs the user the link — safe-by-default."
        ),
    ),
    evidence_json: str = typer.Option("", "--evidence-json", help=_EVIDENCE_JSON_HELP),
    allow_long_review: bool = typer.Option(False, "--allow-long-review", help=_ALLOW_LONG_REVIEW_HELP),
    allow_todo_blocker: bool = typer.Option(False, "--allow-todo-blocker", help=_ALLOW_TODO_BLOCKER_HELP),
) -> None:
    """Post a comment on a GitLab MR — DRAFT by default, ``--live`` requires Slack approval.

    Default behaviour (#1207): create a draft note via the same path as
    ``post-draft-note`` and DM the user the link, so the agent's job
    ends at the draft and the user submits. Pass ``--live`` to publish
    the comment directly — gated on a Slack-recorded
    :class:`~teatree.core.models.live_post_approval.LivePostApproval`
    for the MR (mint via ``t3 review approve-live-post``).

    ``--allow-long-review`` / ``--allow-todo-blocker`` are the documented
    per-post escapes for the colleague-MR shape and TODO-anchor gates
    respectively (#126), mirroring the sibling override flags.
    """
    service = _require_token()
    evidence = _parse_evidence(evidence_json)
    msg, code = service.post_comment(
        repo,
        mr,
        note,
        file=file,
        line=line,
        live=live,
        evidence=evidence,
        allow_long_review=allow_long_review,
        allow_todo_blocker=allow_todo_blocker,
    )
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


# `review_run` (#1206) registers its own command on `review_app` at import time.
# Loaded here, alongside the other typer command bindings, so the review.py
# LOC ceiling (`scripts/hooks/check_module_health.py`) stays satisfied.
from teatree.cli import review_run as _review_run  # noqa: E402, F401 — registration side-effect

__all__ = ["approve", "post_comment", "post_draft_note", "reply_to_discussion", "unapprove"]
