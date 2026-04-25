from pathlib import Path

from teatree.backends.gitlab_api import GitLabAPI, ProjectInfo
from teatree.backends.protocols import PullRequestSpec
from teatree.core.sync import RawAPIDict


def get_client(*, token: str = "", base_url: str = "") -> GitLabAPI:
    """Build a ``GitLabAPI`` from explicit credentials.

    When *token* is empty the ``GitLabAPI`` default (env-var fallback) is used.
    """
    return GitLabAPI(
        token=token,
        base_url=base_url or "https://gitlab.com/api/v4",
    )


class GitLabCodeHost:
    def __init__(
        self,
        *,
        client: GitLabAPI | None = None,
        token: str = "",
        base_url: str = "",
    ) -> None:
        self._client = client or get_client(token=token, base_url=base_url)

    def create_pr(self, spec: PullRequestSpec) -> RawAPIDict:
        project = self._resolve_project(spec.repo)
        if project is None:
            return {"error": f"Could not resolve project: {spec.repo}"}

        payload: RawAPIDict = {
            "source_branch": spec.branch,
            "target_branch": spec.target_branch or project.default_branch,
            "title": spec.title,
            "description": spec.description,
        }
        if spec.labels:
            payload["labels"] = ",".join(spec.labels)
        if spec.assignee:
            payload["assignee_username"] = spec.assignee
        if spec.draft and not spec.title.startswith("Draft:"):
            payload["title"] = f"Draft: {spec.title}"

        return self._client.post_json(f"projects/{project.project_id}/merge_requests", payload) or {}

    def current_user(self) -> str:
        """Return the authenticated GitLab username."""
        return self._client.current_username()

    def list_open_prs(self, repo: str, author: str) -> list[dict[str, object]]:
        project = self._resolve_project(repo)
        if project is None:
            return []

        data = self._client.get_json(
            f"projects/{project.project_id}/merge_requests?state=opened&author_username={author}&per_page=100",
        )
        return data if isinstance(data, list) else []

    def post_mr_note(self, *, repo: str, mr_iid: int, body: str) -> dict[str, object]:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}

        payload = {"body": body}
        return self._client.post_json(f"projects/{project.project_id}/merge_requests/{mr_iid}/notes", payload) or {}

    def update_mr_note(self, *, repo: str, mr_iid: int, note_id: int, body: str) -> dict[str, object]:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}
        return (
            self._client.put_json(
                f"projects/{project.project_id}/merge_requests/{mr_iid}/notes/{note_id}",
                {"body": body},
            )
            or {}
        )

    def list_mr_notes(self, *, repo: str, mr_iid: int) -> list[dict[str, object]]:
        project = self._resolve_project(repo)
        if project is None:
            return []
        data = self._client.get_json(f"projects/{project.project_id}/merge_requests/{mr_iid}/notes?per_page=100")
        return data if isinstance(data, list) else []

    def upload_file(self, *, repo: str, filepath: str) -> dict[str, object]:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}
        return self._client.upload_file(project.project_id, filepath) or {}

    def _resolve_project(self, repo: str) -> ProjectInfo | None:
        """Resolve a GitLab project from a local path, ``namespace/repo`` slug, or bare name.

        Bare repo names (no slash, no matching path) fall back to the CWD's
        git remote — ``Worktree.repo_path`` stores a bare name, and callers
        that hand it straight to the code host would otherwise 404.
        """
        if Path(repo).exists():
            return self._client.resolve_project_from_remote(repo)
        if "/" in repo:
            return self._client.resolve_project(repo)
        return self._client.resolve_project_from_remote(".")
