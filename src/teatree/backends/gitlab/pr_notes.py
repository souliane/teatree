"""An MR's notes and inline review, split out of ``client.py``."""

from collections.abc import Callable

from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
from teatree.backends.gitlab.inline_position import InlinePosition, MrDiff
from teatree.core.backend_protocols import PartialReviewPublishError, PrReview, PrReviewComment
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

    def submit_review(self, *, repo: str, pr_iid: int, review: PrReview) -> RawAPIDict:
        """One positioned discussion per comment, then the summary note when it has text.

        Every position resolves before the first post, so an unanchorable line refuses the whole
        review. The marker rides the LAST post, so a run that dies partway is retried, not skipped.
        """
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}
        mr = f"projects/{project.project_id}/merge_requests/{pr_iid}"
        posts: list[tuple[str, RawAPIDict]] = []
        if review.comments:
            diff = self._reviewed_diff(project, pr_iid, review)
            posts = [
                (f"{mr}/discussions", {"body": item.body, "position": self._position(diff, item)})
                for item in review.comments
            ]
        if review.body.strip():
            posts.append((f"{mr}/notes", {"body": review.body}))
        if not posts:
            return {"error": "the review has nothing to post"}
        endpoint, last = posts[-1]
        posts[-1] = (endpoint, {**last, "body": f"{last['body']}\n\n{review.marker}"})
        return self._post_all(posts)

    def _reviewed_diff(self, project: ProjectInfo, pr_iid: int, review: PrReview) -> MrDiff:
        diff, error = MrDiff.fetch(self._client, str(project.project_id), pr_iid)
        if diff is None:
            raise ValueError(error)
        head = diff.diff_refs["head_sha"]
        if head != review.commit_sha:
            msg = f"MR head moved to {head[:8]} while the review was reached on {review.commit_sha[:8]}"
            raise ValueError(msg)
        return diff

    @staticmethod
    def _position(diff: MrDiff, item: PrReviewComment) -> InlinePosition:
        position, error = diff.position(item.path, item.line)
        if position is None:
            raise ValueError(error)
        return position

    def _post_all(self, posts: list[tuple[str, RawAPIDict]]) -> RawAPIDict:
        result: RawAPIDict = {}
        for landed, (endpoint, payload) in enumerate(posts):
            try:
                result = self._client.post_json(endpoint, payload) or {}
            except Exception as exc:
                if landed:
                    raise PartialReviewPublishError(landed=landed, total=len(posts)) from exc
                raise
        return result
