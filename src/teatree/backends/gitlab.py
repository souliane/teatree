import re
from pathlib import Path
from urllib.parse import urlparse

from teatree.backends.gitlab_api import GitLabAPI, ProjectInfo
from teatree.backends.protocols import PullRequestSpec
from teatree.core.sync import RawAPIDict

_ISSUE_URL_RE = re.compile(r"^/(?P<path>.+?)/-/issues/(?P<iid>\d+)/?$")


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

    def list_my_prs(self, *, author: str) -> list[RawAPIDict]:
        return self._client.list_all_open_mrs(author)

    def list_review_requested_prs(self, *, reviewer: str) -> list[RawAPIDict]:
        return self._client.list_open_mrs_as_reviewer(reviewer)

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        return self._client.list_open_issues_for_assignee(assignee)

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}

        payload = {"body": body}
        return self._client.post_json(f"projects/{project.project_id}/merge_requests/{pr_iid}/notes", payload) or {}

    def update_pr_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> RawAPIDict:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}
        return (
            self._client.put_json(
                f"projects/{project.project_id}/merge_requests/{pr_iid}/notes/{comment_id}",
                {"body": body},
            )
            or {}
        )

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        project = self._resolve_project(repo)
        if project is None:
            return []
        data = self._client.get_json(f"projects/{project.project_id}/merge_requests/{pr_iid}/notes?per_page=100")
        return data if isinstance(data, list) else []

    def upload_file(self, *, repo: str, filepath: str) -> dict[str, object]:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}
        return self._client.upload_file(project.project_id, filepath) or {}

    def get_issue(self, issue_url: str) -> RawAPIDict:
        """Fetch a GitLab issue from its full URL.

        Supports the canonical web format ``https://gitlab.example.com/<group>/<repo>/-/issues/<iid>``.
        Returns ``{"error": ...}`` when the URL is not a recognised GitLab issue URL or when
        the project cannot be resolved.
        """
        path = urlparse(issue_url).path
        match = _ISSUE_URL_RE.match(path)
        if match is None:
            return {"error": f"Not a GitLab issue URL: {issue_url}"}

        project = self._client.resolve_project(match["path"])
        if project is None:
            return {"error": f"Could not resolve project: {match['path']}"}

        issue = self._client.get_issue(project.project_id, int(match["iid"]))
        return issue if isinstance(issue, dict) else {"error": f"Issue not found: {issue_url}"}

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
