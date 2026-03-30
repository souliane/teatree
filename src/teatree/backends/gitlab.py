from pathlib import Path

from django.conf import settings

from teatree.utils.gitlab_api import GitLabAPI, ProjectInfo


def get_client() -> GitLabAPI:
    return GitLabAPI(
        token=getattr(settings, "TEATREE_GITLAB_TOKEN", ""),
        base_url=getattr(settings, "TEATREE_GITLAB_URL", "https://gitlab.com/api/v4"),
    )


class GitLabCodeHost:
    def __init__(self, client: GitLabAPI | None = None) -> None:
        self._client = client or get_client()

    def create_pr(  # noqa: PLR0913
        self,
        *,
        repo: str,
        branch: str,
        title: str,
        description: str,
        target_branch: str = "",
        labels: list[str] | None = None,
    ) -> dict[str, object]:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}

        payload: dict[str, object] = {
            "source_branch": branch,
            "target_branch": target_branch or project.default_branch,
            "title": title,
            "description": description,
        }
        if labels:
            payload["labels"] = ",".join(labels)

        return self._client.post_json(f"projects/{project.project_id}/merge_requests", payload) or {}

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
        if Path(repo).exists():
            return self._client.resolve_project_from_remote(repo)
        return self._client.resolve_project(repo)
