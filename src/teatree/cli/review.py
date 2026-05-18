"""Review CLI commands — GitLab draft note operations."""

import re
from http import HTTPStatus
from typing import TypedDict

import typer

from teatree.utils.run import run_allowed_to_fail


class InlinePosition(TypedDict):
    """GitLab inline-note position payload (text diff anchoring)."""

    position_type: str
    base_sha: str
    head_sha: str
    start_sha: str
    old_path: str
    new_path: str
    new_line: int


review_app = typer.Typer(no_args_is_help=True, help="Code review helpers.")
_TOKEN_PARTS_COUNT = 2
_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_NEARBY_LINE_RANGE = 5
_HTTP_OK_CODES = frozenset({HTTPStatus.OK, HTTPStatus.CREATED, HTTPStatus.NO_CONTENT})


def _on_behalf_gate_active() -> bool:
    """Whether the ask-before-post-on-behalf pre-gate forbids unattended posting.

    An MR approval/unapproval is an outward, state-changing post made under
    the user's identity, so it must respect the same
    ``ask_before_post_on_behalf`` pre-gate the posting subcommands use
    (souliane/teatree#960).

    The gate module (``teatree.on_behalf_gate``) is the single source of
    truth. It is wired here through a soft import so this command works
    whether or not the gate PR has merged yet: if the module is absent the
    gate is treated as inactive (no behaviour change until it lands); once
    present, ``ask_before_post_on_behalf_enabled()`` decides.
    """
    try:
        from teatree.on_behalf_gate import (  # ty: ignore[unresolved-import]  # noqa: PLC0415
            ask_before_post_on_behalf_enabled,
        )
    except ModuleNotFoundError:
        return False
    return ask_before_post_on_behalf_enabled()


def _find_added_line(diff_text: str, target_line: int) -> tuple[bool, list[int]]:
    """Scan a unified-diff hunk text for ``target_line`` in the new file.

    Returns ``(is_added, nearby_added_lines)`` — ``is_added`` is True when the
    target line corresponds to an added (``+``) line in any hunk; the second
    element lists added line numbers within ±5 of the target for error hints.
    """
    is_added = False
    nearby: list[int] = []
    nl: int | None = None
    for line in diff_text.splitlines():
        m = _HUNK_HEADER.match(line)
        if m:
            nl = int(m.group(1))
            continue
        if nl is None:
            continue
        sign = line[:1] if line else " "
        if sign == "-":
            continue
        if sign == "+":
            if nl == target_line:
                is_added = True
            if abs(nl - target_line) <= _NEARBY_LINE_RANGE:
                nearby.append(nl)
        nl += 1
    return is_added, sorted(set(nearby))


class ReviewService:
    """GitLab draft note operations for code review."""

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

    def _fetch_diff_refs(self, encoded_repo: str, mr: int) -> tuple[dict[str, str] | None, str]:
        """Return the MR's diff_refs (base/head/start SHAs) or an error message."""
        api = self._get_api()
        mr_data = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}")
        if not isinstance(mr_data, dict):
            return None, f"Could not fetch MR !{mr}"
        diff_refs_raw = mr_data.get("diff_refs", {})
        if not isinstance(diff_refs_raw, dict):
            return None, "MR has no diff_refs"
        return {str(k): str(v) for k, v in diff_refs_raw.items()}, ""

    def _fetch_file_diff(self, encoded_repo: str, mr: int, file: str) -> tuple[str | None, str]:
        """Return the raw unified diff for ``file`` in the MR, or an error message.

        Uses ``access_raw_diffs=true`` so large files collapsed by the default
        ``/diffs`` endpoint still surface their full hunks.
        """
        api = self._get_api()
        changes = api.get_json(f"projects/{encoded_repo}/merge_requests/{mr}/changes?access_raw_diffs=true")
        if not isinstance(changes, dict):
            return None, "Could not fetch MR changes to validate inline target"
        files = changes.get("changes")
        if not isinstance(files, list):
            return None, "MR changes response had no `changes` array"
        match = next(
            (f for f in files if isinstance(f, dict) and (f.get("new_path") == file or f.get("old_path") == file)),
            None,
        )
        if match is None:
            paths = [str(f.get("new_path")) for f in files if isinstance(f, dict)]
            return None, f"File {file!r} is not changed in MR !{mr}. Changed files: {paths}"
        diff_text = str(match.get("diff") or "")
        if not diff_text:
            return None, (
                f"File {file!r} has no diff content in the MR API response (likely a collapsed large diff). "
                "draft_notes cannot anchor on collapsed files — use `t3 review post-comment` instead, "
                "or pick a smaller file."
            )
        return diff_text, ""

    def _resolve_inline_position(
        self,
        encoded_repo: str,
        mr: int,
        file: str,
        line: int,
    ) -> tuple[InlinePosition | None, str]:
        """Build a GitLab inline-note ``position`` dict, or return an error message.

        Validates that ``file:line`` is an added (``+``) line in the MR diff.
        """
        diff_refs, refs_error = self._fetch_diff_refs(encoded_repo, mr)
        if diff_refs is None:
            return None, refs_error
        diff_text, diff_error = self._fetch_file_diff(encoded_repo, mr, file)
        if diff_text is None:
            return None, diff_error
        is_added, nearby = _find_added_line(diff_text, line)
        if not is_added:
            hint = f" Nearby added lines in this file: {nearby}." if nearby else ""
            return None, (
                f"Line {line} in {file} is not an added (`+`) line in the MR diff — "
                f"inline notes only anchor on added lines.{hint}"
            )
        position: InlinePosition = {
            "position_type": "text",
            "base_sha": diff_refs["base_sha"],
            "head_sha": diff_refs["head_sha"],
            "start_sha": diff_refs["start_sha"],
            "old_path": file,
            "new_path": file,
            "new_line": line,
        }
        return position, ""

    def post_draft_note(self, repo: str, mr: int, note: str, *, file: str = "", line: int = 0) -> tuple[str, int]:
        """Post a draft note. Returns (message, exit_code).

        For inline notes (file+line), validates that the target line is an added
        (``+``) line in the MR diff, then verifies after posting that GitLab
        actually anchored the draft (``line_code`` non-null). Broken drafts
        (anchor refused, usually because the file diff is collapsed) are
        deleted and surfaced as an error so they cannot be published silently.
        """
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        endpoint = f"projects/{encoded}/merge_requests/{mr}/draft_notes"

        if not (file and line):
            result = api.post_json(endpoint, {"note": note})
            if not result:
                return "Failed to post draft note", 1
            return f"OK draft_note_id={dict(result).get('id')}", 0

        position, error = self._resolve_inline_position(encoded, mr, file, line)
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
        """
        api = self._get_api()
        encoded = repo.replace("/", "%2F")

        if not (file and line):
            result = api.post_json(f"projects/{encoded}/merge_requests/{mr}/notes", {"body": note})
            if not result:
                return "Failed to post comment", 1
            return f"OK note_id={dict(result).get('id')}", 0

        position, error = self._resolve_inline_position(encoded, mr, file, line)
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

    def delete_draft_note(self, repo: str, mr: int, note_id: int) -> tuple[str, int]:
        """Delete a draft note. Returns (message, exit_code)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        status = api.delete(f"projects/{encoded}/merge_requests/{mr}/draft_notes/{note_id}")
        if status == HTTPStatus.NO_CONTENT:
            return f"OK deleted draft_note_id={note_id}", 0
        return f"Failed: HTTP {status}", 1

    def publish_draft_notes(self, repo: str, mr: int) -> tuple[str, int]:
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/draft_notes/bulk_publish")
        if status in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
            return "OK — all draft notes published", 0
        return f"Failed: HTTP {status}", 1

    def reply_to_discussion(self, repo: str, mr: int, discussion_id: str, body: str) -> tuple[str, int]:
        """Reply to an existing discussion thread on an MR. Returns (message, exit_code)."""
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
        """Mark a discussion thread resolved or unresolved. Returns (message, exit_code)."""
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
        """
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

    def _identity_has_reviewed(self, encoded_repo: str, mr: int) -> tuple[bool, str]:
        """Whether the approving identity already authored a note on this MR.

        Encodes the review-before-approve doctrine: an approval may only be
        recorded once the same identity has left a reviewing footprint
        (any note in any discussion thread). Returns ``(reviewed, error)``;
        ``error`` is non-empty only when the identity itself cannot be
        resolved (a hard precondition failure, not "no review yet").
        """
        api = self._get_api()
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

    def approve(self, repo: str, mr: int) -> tuple[str, int]:
        """Approve an MR — refuses unless the identity has already reviewed it.

        Returns (message, exit_code). The review-first precondition encodes
        the approve-on-review doctrine: an approval cannot be recorded
        without a prior reviewing footprint from the same identity.
        """
        encoded = repo.replace("/", "%2F")
        reviewed, error = self._identity_has_reviewed(encoded, mr)
        if error:
            return error, 1
        if not reviewed:
            msg = (
                f"Refusing to approve !{mr}: review before approve — no review note authored by your "
                "identity exists on this MR yet. Post a review (`t3 review post-comment` / "
                "`post-draft-note`) first, then approve."
            )
            return msg, 1
        api = self._get_api()
        status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/approve")
        if status in _HTTP_OK_CODES:
            return f"OK approved !{mr}", 0
        return f"Failed: HTTP {status}", 1

    def unapprove(self, repo: str, mr: int) -> tuple[str, int]:
        """Revoke this identity's approval on an MR. Returns (message, exit_code).

        No review-first precondition — removing an approval is the safe
        direction and must always be reachable.
        """
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        status = api.post_status(f"projects/{encoded}/merge_requests/{mr}/unapprove")
        if status in _HTTP_OK_CODES:
            return f"OK unapproved !{mr}", 0
        return f"Failed: HTTP {status}", 1


def _refuse_if_on_behalf_gated() -> None:
    """Refuse an approval/unapproval when the on-behalf pre-gate is active."""
    if _on_behalf_gate_active():
        typer.echo(
            "Refusing: `ask_before_post_on_behalf` is enabled — an MR approval is an outward "
            "post on your behalf and must be user-approved first. Disable the gate per-overlay "
            "in ~/.teatree.toml once you trust the workflow, or record the approval manually.",
        )
        raise typer.Exit(code=1)


def _require_token() -> ReviewService:
    token = ReviewService.get_gitlab_token()
    if not token:
        typer.echo("No GitLab token found. Run: glab auth login")
        raise typer.Exit(code=1)
    return ReviewService(token)


@review_app.command(name="post-draft-note")
def post_draft_note(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
    note: str = typer.Argument(help="Comment text (markdown)"),
    file: str = typer.Option("", help="File path for inline comment (omit for general note)"),
    line: int = typer.Option(0, help="Line number in the new file (must be an added line)"),
) -> None:
    """Post a draft note on a GitLab MR (inline or general)."""
    service = _require_token()
    msg, code = service.post_draft_note(repo, mr, note, file=file, line=line)
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


@review_app.command(name="delete-draft-note")
def delete_draft_note(
    repo: str = typer.Argument(help="GitLab project path"),
    mr: int = typer.Argument(help="Merge request IID"),
    note_id: int = typer.Argument(help="Draft note ID to delete"),
) -> None:
    """Delete a draft note from a GitLab MR."""
    service = _require_token()
    msg, code = service.delete_draft_note(repo, mr, note_id)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


@review_app.command(name="publish-draft-notes")
def publish_draft_notes(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
) -> None:
    """Publish all draft notes on a GitLab MR (bulk submit)."""
    service = _require_token()
    msg, code = service.publish_draft_notes(repo, mr)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


@review_app.command(name="list-draft-notes")
def list_draft_notes(
    repo: str = typer.Argument(help="GitLab project path"),
    mr: int = typer.Argument(help="Merge request IID"),
) -> None:
    """List draft notes on a GitLab MR."""
    service = _require_token()
    msg, _code = service.list_draft_notes(repo, mr)
    typer.echo(msg)


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


@review_app.command(name="update-note")
def update_note(
    repo: str = typer.Argument(help="GitLab project path (e.g., my-org/my-repo)"),
    mr: int = typer.Argument(help="Merge request IID"),
    note_id: int = typer.Argument(help="Note ID (draft or published)"),
    body: str = typer.Argument(help="New comment body (markdown)"),
) -> None:
    """Update a note on a GitLab MR — auto-detects draft vs published."""
    service = _require_token()
    msg, code = service.update_note(repo, mr, note_id, body)
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
    already exist on the MR (review before approve). Also respects the
    `ask_before_post_on_behalf` pre-gate (souliane/teatree#960).
    """
    _refuse_if_on_behalf_gated()
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

    No review precondition (revoking is the safe direction). Respects the
    `ask_before_post_on_behalf` pre-gate (souliane/teatree#960).
    """
    _refuse_if_on_behalf_gated()
    service = _require_token()
    msg, code = service.unapprove(repo, mr)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)


@review_app.command(name="resolve-discussion")
def resolve_discussion(
    repo: str = typer.Argument(help="GitLab project path"),
    mr: int = typer.Argument(help="Merge request IID"),
    discussion_id: str = typer.Argument(help="Discussion (thread) ID"),
    *,
    resolved: bool = typer.Option(True, "--resolved/--no-resolved", help="Mark resolved (default) or re-open."),
) -> None:
    """Mark a GitLab MR discussion thread resolved or unresolved."""
    service = _require_token()
    msg, code = service.resolve_discussion(repo, mr, discussion_id, resolved=resolved)
    typer.echo(msg)
    if code:
        raise typer.Exit(code=code)
