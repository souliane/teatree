"""Review CLI commands — GitLab draft note operations."""

import subprocess  # noqa: S404
from http import HTTPStatus

import typer

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
        result = subprocess.run(
            ["glab", "auth", "status", "-t"],
            capture_output=True,
            text=True,
            check=False,
        )
        for line in result.stderr.splitlines():
            if "Token" in line and ":" in line:
                token_value = line.rsplit(":", 1)[-1].strip()
                if token_value:
                    return token_value
        return ""

    def _get_api(self):  # noqa: ANN202
        from teatree.backends.gitlab_api import GitLabAPI  # noqa: PLC0415

        return GitLabAPI(token=self.token)

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
        if file and line and not line_code:
            parts.insert(0, f"WARNING: line_code is null — note may not render inline (line {line} in {file}).")

        return "\n".join(parts), 0

    def delete_draft_note(self, repo: str, mr: int, note_id: int) -> tuple[str, int]:
        """Delete a draft note. Returns (message, exit_code)."""
        import httpx  # noqa: PLC0415

        encoded = repo.replace("/", "%2F")
        response = httpx.delete(
            f"https://gitlab.com/api/v4/projects/{encoded}/merge_requests/{mr}/draft_notes/{note_id}",
            headers={"PRIVATE-TOKEN": self.token},
            timeout=10.0,
        )
        if response.status_code == HTTPStatus.NO_CONTENT:
            return f"OK deleted draft_note_id={note_id}", 0
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


@review_app.command(name="list-draft-notes")
def list_draft_notes(
    repo: str = typer.Argument(help="GitLab project path"),
    mr: int = typer.Argument(help="Merge request IID"),
) -> None:
    """List draft notes on a GitLab MR."""
    service = _require_token()
    msg, _code = service.list_draft_notes(repo, mr)
    typer.echo(msg)
