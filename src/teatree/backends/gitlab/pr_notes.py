"""An MR's notes and inline review, split out of ``client.py``."""

from collections.abc import Callable

from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.backends.gitlab.inline_position import resolve_inline_position
from teatree.core.backend_protocols import PrReviewComment
from teatree.types import RawAPIDict


class GitLabMrNotes:
    def __init__(self, client: GitLabAPI, resolve_project: Callable[[str], ProjectInfo | None]) -> None:
        self._client = client
        self._resolve_project = resolve_project

    def post_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}
        payload: RawAPIDict = {"body": body}
        return self._client.post_json(f"projects/{project.project_id}/merge_requests/{pr_iid}/notes", payload) or {}

    def update_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> RawAPIDict:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}
        endpoint = f"projects/{project.project_id}/merge_requests/{pr_iid}/notes/{comment_id}"
        return self._client.put_json(endpoint, {"body": body}) or {}

    def list_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        project = self._resolve_project(repo)
        if project is None:
            return []
        return self._client.get_json_paginated(
            f"projects/{project.project_id}/merge_requests/{pr_iid}/notes?per_page=100"
        )

    def find_review(self, *, repo: str, pr_iid: int, marker: str) -> bool:
        return any(marker in str(note.get("body") or "") for note in self.list_comments(repo=repo, pr_iid=pr_iid))

    def submit_review(
        self, *, repo: str, pr_iid: int, head_sha: str, summary: str, comments: list[PrReviewComment]
    ) -> RawAPIDict:
        """One inline discussion per comment, then the summary note LAST.

        The dedup marker rides the summary, so a run that dies partway is retried rather than
        skipped. Every position is resolved before the first post, so an unanchorable line
        refuses the whole review instead of leaving half of it on the MR.
        """
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}
        positions = []
        for item in comments:
            position, error = resolve_inline_position(
                self._client, str(project.project_id), pr_iid, item.path, item.line
            )
            if position is None:
                raise ValueError(error)
            if position["head_sha"] != head_sha:
                msg = f"MR head moved to {position['head_sha'][:8]} while the review was reached on {head_sha[:8]}"
                raise ValueError(msg)
            positions.append(position)
        for item, position in zip(comments, positions, strict=True):
            self._client.post_json(
                f"projects/{project.project_id}/merge_requests/{pr_iid}/discussions",
                {"body": item.body, "position": position},
            )
        return self.post_comment(repo=repo, pr_iid=pr_iid, body=summary)
