import re
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import urlparse

from teatree.backends import gitlab_api as _gitlab_api
from teatree.backends.gitlab_api import GitLabAPI, ProjectInfo
from teatree.backends.protocols import PullRequestSpec, ReviewState
from teatree.types import RawAPIDict

_ISSUE_URL_RE = re.compile(r"^/(?P<path>.+?)/-/issues/(?P<iid>\d+)/?$")
_MR_URL_RE = re.compile(r"^/(?P<path>.+?)/-/merge_requests/(?P<iid>\d+)/?$")
# GitLab serves the same iid under both /-/issues/<iid> and /-/work_items/<iid>;
# the notes API endpoint is identical for either.
_ISSUE_OR_WORKITEM_URL_RE = re.compile(r"^/(?P<path>.+?)/-/(?:issues|work_items)/(?P<iid>\d+)/?$")


class _GitLabUser(TypedDict, total=False):
    """Subset of the GitLab user payload that teatree reads."""

    username: str


class _GitLabMergeRequestSummary(TypedDict, total=False):
    """Subset of the GitLab MR response read for the review state lookup."""

    reviewers: list[_GitLabUser]


def get_client(*, token: str = "", base_url: str = "") -> GitLabAPI:
    """Build a ``GitLabAPI`` from explicit credentials.

    When *token* is empty the ``GitLabAPI`` default (env-var fallback) is used.
    Resolves ``GitLabAPI`` through the module attribute so test patches that
    rebind ``teatree.backends.gitlab_api.GitLabAPI`` apply here too.
    """
    return _gitlab_api.GitLabAPI(
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

    @property
    def client(self) -> GitLabAPI:
        """Underlying ``GitLabAPI``.

        Exposed for ``GitLabSyncBackend`` and other GitLab-specific consumers
        that need calls outside the cross-host Protocol surface (pipeline
        status, approvals, discussions, draft notes count, terminal-state PR
        scans). Cross-host code paths must keep using the Protocol methods.
        """
        return self._client

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

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        return self._client.list_all_open_mrs(author, updated_after=updated_after)

    def list_review_requested_prs(
        self,
        *,
        reviewer: str,
        updated_after: str | None = None,
    ) -> list[RawAPIDict]:
        return self._client.list_open_mrs_as_reviewer(reviewer, updated_after=updated_after)

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

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        """Return *reviewer*'s current review state on the MR at *pr_url*.

        GitLab does not expose a per-reviewer review timeline, so we infer
        state from the MR's approval list and reviewer assignment. When the
        reviewer's username is in ``approved_by``, they are ``APPROVED``;
        when they are still a requested reviewer but not in ``approved_by``
        they are ``PENDING`` (a fresh re-request, or an approval that the
        forge dropped on force-push). Unparsable URLs yield ``NONE``.
        """
        path = urlparse(pr_url).path
        match = _MR_URL_RE.match(path)
        if match is None or not reviewer:
            return ReviewState.NONE

        project = self._client.resolve_project(match["path"])
        if project is None:
            return ReviewState.NONE

        approvals = self._client.get_mr_approvals(project.project_id, int(match["iid"]))
        approved_by = approvals.get("approved_by")
        if isinstance(approved_by, list) and reviewer in approved_by:
            return ReviewState.APPROVED

        mr = self._client.get_json(f"projects/{project.project_id}/merge_requests/{match['iid']}")
        if isinstance(mr, dict):
            reviewers = cast("_GitLabMergeRequestSummary", mr).get("reviewers")
            if isinstance(reviewers, list):
                for entry in reviewers:
                    if isinstance(entry, dict) and entry.get("username") == reviewer:
                        return ReviewState.PENDING
        return ReviewState.NONE

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

    def post_issue_comment(self, *, issue_url: str, body: str) -> RawAPIDict:
        """Post a comment to a GitLab issue or work item.

        Accepts the canonical web formats
        ``https://gitlab.example.com/<group>/<repo>/-/issues/<iid>`` and
        ``…/-/work_items/<iid>`` (GitLab exposes the same iid under both).
        Returns ``{"error": ...}`` when the URL is not a recognised GitLab
        issue URL or when the project cannot be resolved.
        """
        path = urlparse(issue_url).path
        match = _ISSUE_OR_WORKITEM_URL_RE.match(path)
        if match is None:
            return {"error": f"Not a GitLab issue URL: {issue_url}"}

        project = self._client.resolve_project(match["path"])
        if project is None:
            return {"error": f"Could not resolve project: {match['path']}"}

        return (
            self._client.post_json(
                f"projects/{project.project_id}/issues/{int(match['iid'])}/notes",
                {"body": body},
            )
            or {}
        )

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
