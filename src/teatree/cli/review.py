"""Review CLI commands — GitLab draft note operations."""

from http import HTTPStatus

import httpx
import typer

from teatree.utils.run import run_allowed_to_fail

review_app = typer.Typer(no_args_is_help=True, help="Code review helpers.")
_TOKEN_PARTS_COUNT = 2


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

    def post_draft_note(self, repo: str, mr: int, note: str, *, file: str = "", line: int = 0) -> tuple[str, int]:
        """Post a draft note. Returns (message, exit_code)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")

        if file and line:
            mr_data = api.get_json(f"projects/{encoded}/merge_requests/{mr}")
            if not isinstance(mr_data, dict):
                return f"Could not fetch MR !{mr} from {repo}", 1

            diff_refs_raw = mr_data.get("diff_refs", {})
            if not isinstance(diff_refs_raw, dict):
                return "MR has no diff_refs", 1
            diff_refs: dict[str, str] = {str(k): str(v) for k, v in diff_refs_raw.items()}

            payload: dict[str, object] = {
                "note": note,
                "position": {
                    "position_type": "text",
                    "base_sha": diff_refs["base_sha"],
                    "head_sha": diff_refs["head_sha"],
                    "start_sha": diff_refs["start_sha"],
                    "old_path": file,
                    "new_path": file,
                    "new_line": line,
                },
            }
        else:
            payload = {"note": note}

        result = api.post_json(f"projects/{encoded}/merge_requests/{mr}/draft_notes", payload)
        if not result:
            return "Failed to post draft note", 1

        result_dict = dict(result) if isinstance(result, dict) else {}
        note_id = result_dict.get("id")
        position_raw = result_dict.get("position")
        position = dict(position_raw) if isinstance(position_raw, dict) else {}
        line_code = position.get("line_code")

        parts = [f"OK draft_note_id={note_id}"]
        if line_code:
            parts.append(f"line_code={line_code}")
        position_stored = bool(position.get("new_path"))
        if file and line and not position_stored:
            parts.insert(0, f"WARNING: inline position was not accepted by GitLab (line {line} in {file}).")

        return "\n".join(parts), 0

    def delete_draft_note(self, repo: str, mr: int, note_id: int) -> tuple[str, int]:
        """Delete a draft note. Returns (message, exit_code)."""
        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        response = httpx.delete(
            f"{api.base_url}/projects/{encoded}/merge_requests/{mr}/draft_notes/{note_id}",
            headers={"PRIVATE-TOKEN": self.token},
            timeout=10.0,
        )
        if response.status_code == HTTPStatus.NO_CONTENT:
            return f"OK deleted draft_note_id={note_id}", 0
        return f"Failed: HTTP {response.status_code}", 1

    def publish_draft_notes(self, repo: str, mr: int) -> tuple[str, int]:

        api = self._get_api()
        encoded = repo.replace("/", "%2F")
        response = httpx.post(
            f"{api.base_url}/projects/{encoded}/merge_requests/{mr}/draft_notes/bulk_publish",
            headers={"PRIVATE-TOKEN": self.token},
            timeout=10.0,
        )
        if response.status_code in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
            return "OK — all draft notes published", 0
        return f"Failed: HTTP {response.status_code}", 1

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
        response = httpx.put(
            f"{api.base_url}/projects/{encoded}/merge_requests/{mr}/discussions/{discussion_id}?resolved={flag}",
            headers={"PRIVATE-TOKEN": self.token},
            timeout=10.0,
        )
        if response.status_code in {HTTPStatus.OK, HTTPStatus.NO_CONTENT}:
            return f"OK resolved={resolved}", 0
        return f"Failed: HTTP {response.status_code}", 1

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
