"""Review CLI commands — GitLab draft note operations.

Every publishing method (``post_*`` / ``reply_*`` / ``resolve_*`` /
``publish_*`` / ``update_*`` / ``approve`` / ``unapprove`` /
``delete_discussion``) routes through the tri-state
``on_behalf_post_mode`` pre-gate (#960/#1013) the reply transport uses;
read-only methods (``list_draft_notes``, ``delete_draft_note``) bypass
it. Under IMMEDIATE the gate is off; under ASK every method is gated;
under DRAFT_OR_ASK (default) ``post_draft_note`` publishes autonomously
and the agent DMs the user with publish/delete commands, every other
method is gated identically to ASK.

``delete_discussion`` IS gated even though it is the deletion-shaped
sibling of ``delete_draft_note`` — it removes a *published* note that
colleagues can already see, so the removal itself is an on-behalf
colleague-visible mutation. Mirrors the ``update_note`` gating shape
exactly.

The gate is satisfiable without a TTY via a recorded
:class:`~teatree.core.models.on_behalf_approval.OnBehalfApproval`
scoped to ``(<repo>!<mr>, <method_name>)`` — the next matching
invocation publishes and consumes the row.
"""

from http import HTTPStatus

import typer

from teatree.cli import review_approvals
from teatree.cli.review_approvals import register as _register_approvals
from teatree.cli.review_diff import find_added_line, resolve_inline_position
from teatree.cli.review_drafts import register as _register_drafts
from teatree.cli.review_on_behalf import check_on_behalf, on_behalf_gate_active
from teatree.cli.review_on_behalf import register as _register_on_behalf
from teatree.utils.run import run_allowed_to_fail

# Re-exports — keep monkeypatch targets under the ``review`` namespace
# after extraction to :mod:`teatree.cli.review_diff` /
# :mod:`teatree.cli.review_on_behalf` for module-health LOC reasons.
_find_added_line = find_added_line
_on_behalf_gate_active = on_behalf_gate_active

review_app = typer.Typer(no_args_is_help=True, help="Code review helpers.")
_TOKEN_PARTS_COUNT = 2


class ReviewService:
    """GitLab draft note operations for code review.

    Every method that publishes to an MR (post comment, post draft note,
    publish drafts, reply, resolve, update note, approve, unapprove,
    delete discussion) is wrapped by the recorded-approval on-behalf
    pre-gate. See module docstring for the full contract.
    """

    def __init__(self, token: str) -> None:
        self.token = token

    @staticmethod
    def get_gitlab_token() -> str:
        """Extract GitLab token from glab auth or GITLAB_TOKEN env var."""
        import os  # noqa: PLC0415

        token = os.environ.get("GITLAB_TOKEN", "")
        if token:
            return token
        result = run_allowed_to_fail(["glab", "auth", "status", "-t"], expected_codes=None)
        for line in result.stderr.splitlines():
            if "Token" in line and ":" in line:
                token_value = line.rsplit(":", 1)[-1].strip()
                if token_value:
                    return token_value
        return ""

    def _get_api(self):  # noqa: ANN202
        from teatree.backends.gitlab_api import GitLabAPI  # noqa: PLC0415

        return GitLabAPI(token=self.token, base_url=self._resolve_base_url())

    @staticmethod
    def _resolve_base_url() -> str:
        """Resolve GitLab API base URL from overlay config or env, defaulting to gitlab.com."""
        import os  # noqa: PLC0415

        try:
            from teatree.core.overlay_loader import get_overlay  # noqa: PLC0415

            return get_overlay().config.gitlab_url
        except Exception:  # noqa: BLE001
            return os.environ.get("GITLAB_URL", "https://gitlab.com/api/v4")

    def _post_draft_note_impl(self, repo: str, mr: int, note: str, *, file: str, line: int) -> tuple[str, int]:
        """The pre-gate-passed body of :meth:`post_draft_note` (see docstring)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        endpoint = f"projects/{encoded}/merge_requests/{mr}/draft_notes"

        if not (file and line):
            result = api.post_json(endpoint, {"note": note})
            if not result:
                return "Failed to post draft note", 1
            return f"OK draft_note_id={dict(result).get('id')}", 0

        position, error = resolve_inline_position(api, encoded, mr, file, line)
        if position is None:
            return error, 1

        result = api.post_json(endpoint, {"note": note, "position": position})
        if not result:
            return "Failed to post draft note", 1
        result_dict = dict(result) if isinstance(result, dict) else {}
        note_id = result_dict.get("id")
        line_code = result_dict.get("line_code")
        if line_code:
            return f"OK draft_note_id={note_id}\nline_code={line_code}", 0

        if isinstance(note_id, int):
            api.delete(f"{endpoint}/{note_id}")
        return (
            f"GitLab refused to anchor the draft on {file}:{line} (line_code came back null). "
            "This usually means the file diff is collapsed because of its size; the draft_notes "
            "API cannot anchor on collapsed-diff files. Workaround: "
            f"`t3 review post-comment {repo} {mr} ... --file {file} --line {line}` "
            "(creates an immediate non-draft inline discussion)."
        ), 1

    def post_draft_note(self, repo: str, mr: int, note: str, *, file: str = "", line: int = 0) -> tuple[str, int]:
        """Post a draft note. Returns (message, exit_code).

        For inline notes (file+line), validates that the target line is an added
        (``+``) line in the MR diff, then verifies after posting that GitLab
        actually anchored the draft (``line_code`` non-null). Broken drafts
        (anchor refused, usually because the file diff is collapsed) are
        deleted and surfaced as an error so they cannot be published silently.

        Gated by ``on_behalf_post_mode`` (#960). Under
        :attr:`~teatree.config.OnBehalfPostMode.IMMEDIATE` posts directly.
        Under :attr:`~teatree.config.OnBehalfPostMode.DRAFT_OR_ASK` (the
        new default) posts the draft autonomously and DMs the user with
        the publish/delete commands — drafts are colleague-invisible and
        revocable, so the post proceeds without a recorded approval.
        Under :attr:`~teatree.config.OnBehalfPostMode.ASK` the call is
        refused without any GitLab side effect when no recorded
        :class:`OnBehalfApproval` matches
        ``(<repo>!<mr>, "post_draft_note")``.
        """
        blocked = check_on_behalf(repo, mr, "post_draft_note")
        if blocked:
            return blocked, 1
        return self._post_draft_note_impl(repo, mr, note, file=file, line=line)

    def _post_comment_impl(
        self,
        repo: str,
        mr: int,
        note: str,
        *,
        file: str,
        line: int,
    ) -> tuple[str, int]:
        """The pre-gate-passed body of :meth:`post_comment` (see docstring)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")

        if not (file and line):
            result = api.post_json(f"projects/{encoded}/merge_requests/{mr}/notes", {"body": note})
            if not result:
                return "Failed to post comment", 1
            return f"OK note_id={dict(result).get('id')}", 0

        position, error = resolve_inline_position(api, encoded, mr, file, line)
        if position is None:
            return error, 1

        result = api.post_json(
            f"projects/{encoded}/merge_requests/{mr}/discussions",
            {"body": note, "position": position},
        )
        if not result:
            return "Failed to post comment", 1
        result_dict = dict(result) if isinstance(result, dict) else {}
        discussion_id = result_dict.get("id")
        notes = result_dict.get("notes")
        first_note = notes[0] if isinstance(notes, list) and notes else {}
        note_type = first_note.get("type") if isinstance(first_note, dict) else None
        if note_type != "DiffNote":
            return f"Comment posted but not anchored inline (type={note_type!r}). discussion_id={discussion_id}", 1
        return f"OK discussion_id={discussion_id} (inline DiffNote)", 0

    def post_comment(
        self,
        repo: str,
        mr: int,
        note: str,
        *,
        file: str = "",
        line: int = 0,
    ) -> tuple[str, int]:
        """Post an immediate (non-draft) MR comment via ``/discussions``.

        Use when ``post_draft_note`` fails because the file diff is collapsed
        — the discussions endpoint anchors inline notes even on large files,
        but the comment posts immediately instead of batching with a review.

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): the call is refused
        without any GitLab side effect when the gate is on and no recorded
        :class:`OnBehalfApproval` matches ``(<repo>!<mr>, "post_comment")``.
        """
        blocked = check_on_behalf(repo, mr, "post_comment")
        if blocked:
            return blocked, 1
        return self._post_comment_impl(repo, mr, note, file=file, line=line)

    def delete_draft_note(self, repo: str, mr: int, note_id: int) -> tuple[str, int]:
        """Delete a draft note. Returns (message, exit_code)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        status = api.delete(f"projects/{encoded}/merge_requests/{mr}/draft_notes/{note_id}")
        if status == HTTPStatus.NO_CONTENT:
            return f"OK deleted draft_note_id={note_id}", 0
        return f"Failed: HTTP {status}", 1

    def publish_draft_notes(self, repo: str, mr: int) -> tuple[str, int]:
        """Bulk-publish every draft note on an MR.

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): the bulk publish is
        the moment drafts become visible to colleagues, so it routes
        through the same recorded-approval gate every other on-behalf
        post uses.
        """
        blocked = check_on_behalf(repo, mr, "publish_draft_notes")
        if blocked:
            return blocked, 1
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/draft_notes/bulk_publish")
        if status in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
            return "OK — all draft notes published", 0
        return f"Failed: HTTP {status}", 1

    def reply_to_discussion(self, repo: str, mr: int, discussion_id: str, body: str) -> tuple[str, int]:
        """Reply to an existing discussion thread on an MR. Returns (message, exit_code).

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): the reply is refused
        without any GitLab side effect when the gate is on and no recorded
        :class:`OnBehalfApproval` matches ``(<repo>!<mr>, "reply_to_discussion")``.
        """
        blocked = check_on_behalf(repo, mr, "reply_to_discussion")
        if blocked:
            return blocked, 1
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        result = api.post_json(
            f"projects/{encoded}/merge_requests/{mr}/discussions/{discussion_id}/notes",
            {"body": body},
        )
        if not result:
            return "Failed to post reply", 1
        note_id = dict(result).get("id")
        return f"OK reply_note_id={note_id}", 0

    def resolve_discussion(self, repo: str, mr: int, discussion_id: str, *, resolved: bool = True) -> tuple[str, int]:
        """Mark a discussion thread resolved or unresolved. Returns (message, exit_code).

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): a resolve flip is
        visible to colleagues (it closes the discussion under the user's
        identity), so it routes through the same recorded-approval gate.
        """
        blocked = check_on_behalf(repo, mr, "resolve_discussion")
        if blocked:
            return blocked, 1
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        flag = "true" if resolved else "false"
        status = api.put_status(f"projects/{encoded}/merge_requests/{mr}/discussions/{discussion_id}?resolved={flag}")
        if status in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
            return f"OK resolved={resolved}", 0
        return f"Failed: HTTP {status}", 1

    def update_note(self, repo: str, mr: int, note_id: int, body: str) -> tuple[str, int]:
        """Update a note (draft or published) on an MR.

        Tries draft-notes first; falls back to published-notes on 404.

        Gated by ``on_behalf_post_mode`` (#960, BLOCK under `ask` / `draft_or_ask`): an update to a
        *published* note is a colleague-visible edit; the gate covers
        both fallback paths uniformly so a published-note edit cannot
        slip through while a comment-create would be blocked.
        """
        blocked = check_on_behalf(repo, mr, "update_note")
        if blocked:
            return blocked, 1
        api = self._get_api()
        encoded = repo.replace("/", "%2F")

        draft_status = api.put_status(
            f"projects/{encoded}/merge_requests/{mr}/draft_notes/{note_id}",
            {"note": body},
        )
        if draft_status == HTTPStatus.OK:
            return f"OK updated draft_note_id={note_id}", 0
        if draft_status != HTTPStatus.NOT_FOUND:
            return f"Failed (draft): HTTP {draft_status}", 1

        pub_status = api.put_status(
            f"projects/{encoded}/merge_requests/{mr}/notes/{note_id}",
            {"body": body},
        )
        if pub_status == HTTPStatus.OK:
            return f"OK updated note_id={note_id}", 0
        return f"Failed: HTTP {pub_status}", 1

    def delete_discussion(self, repo: str, mr: int, note_id: int) -> tuple[str, int]:
        """Delete a *published* note from an MR. Returns (message, exit_code).

        Use to clean up a published general discussion that should have
        been inline (or any other published note that needs removal).
        Distinct from :meth:`delete_draft_note`, which removes the user's
        own unpublished draft — that is not a colleague-visible mutation
        and stays ungated; this one is.

        Gated by ``ask_before_post_on_behalf`` (#960): the call is refused
        without any GitLab side effect when the gate is on and no recorded
        :class:`OnBehalfApproval` matches ``(<repo>!<mr>, "delete_discussion")``.
        """
        blocked = check_on_behalf(repo, mr, "delete_discussion")
        if blocked:
            return blocked, 1
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        status = api.delete(f"projects/{encoded}/merge_requests/{mr}/notes/{note_id}")
        if status == HTTPStatus.NO_CONTENT:
            return f"OK deleted note_id={note_id}", 0
        return f"Failed: HTTP {status}", status

    def list_draft_notes(self, repo: str, mr: int) -> tuple[str, int]:
        """List draft notes. Returns (message, exit_code)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        notes = api.get_json(f"projects/{encoded}/merge_requests/{mr}/draft_notes")
        if not isinstance(notes, list):
            return "No draft notes found", 0

        lines = []
        for n in notes:
            if not isinstance(n, dict):
                continue
            entry: dict[str, object] = n
            nid = entry.get("id")
            pos_raw = entry.get("position")
            pos = dict(pos_raw) if isinstance(pos_raw, dict) else {}
            fp = pos.get("new_path", "")
            ln = pos.get("new_line", "")
            body = str(entry.get("note", ""))[:60]
            lines.append(f"  {nid}  {fp}:{ln}  {body}...")
        return "\n".join(lines), 0

    def approve(self, repo: str, mr: int) -> tuple[str, int]:
        """Approve an MR — refuses unless the identity has already reviewed it.

        Delegates to :func:`teatree.cli.review_approvals.approve` for the
        implementation body; see that module for the full review-first
        precondition and on-behalf-gate contract.
        """
        return review_approvals.approve(self, repo, mr)

    def unapprove(self, repo: str, mr: int) -> tuple[str, int]:
        """Revoke this identity's approval on an MR. Returns (message, exit_code).

        Delegates to :func:`teatree.cli.review_approvals.unapprove`; see
        that module for the on-behalf-gate contract.
        """
        return review_approvals.unapprove(self, repo, mr)


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


# Register sibling-module typer commands. Kept out of this file so the
# OOP/LOC ceiling (`scripts/hooks/check_module_health.py`) stays
# satisfied — see `teatree.cli.review_on_behalf`,
# `teatree.cli.review_drafts`, and `teatree.cli.review_approvals`.
_register_on_behalf(review_app)
_register_drafts(review_app)
_register_approvals(review_app)
