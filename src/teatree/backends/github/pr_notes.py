"""A GitHub PR's conversation notes and submitted reviews, split out of ``client.py``."""

import json
from typing import cast

from teatree.backends.github.api import _gh_api_patch, _gh_api_post, _run_gh
from teatree.backends.github.api import gh_api_get_paginated as _gh_api_get_paginated
from teatree.backends.github.claims import record_github_note_claim
from teatree.core.backend_protocols import PrReview
from teatree.types import RawAPIDict


class GitHubPrNotes:
    def __init__(self, token: str) -> None:
        self._token = token

    @staticmethod
    def list_discussions(*, repo: str, pr_iid: int) -> list[RawAPIDict]:
        _ = (repo, pr_iid)
        return []

    def resolve_thread(self, *, thread_id: str) -> RawAPIDict:
        query = (
            "mutation($threadId: ID!) { resolveReviewThread(input: {threadId: $threadId}) "
            "{ thread { id isResolved } } }"
        )
        result = _run_gh(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"threadId={thread_id}",
            token=self._token,
            timeout=60,
        )
        payload = json.loads(result.stdout or "{}")
        thread = payload.get("data", {}).get("resolveReviewThread", {}).get("thread")
        if not isinstance(thread, dict) or thread.get("id") != thread_id or thread.get("isResolved") is not True:
            message = "Review thread resolution not confirmed"
            raise RuntimeError(message)
        return thread

    def post_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        data = _gh_api_post(f"repos/{repo}/issues/{pr_iid}/comments", {"body": body}, token=self._token)
        result: RawAPIDict = cast("RawAPIDict", data) if isinstance(data, dict) else {}
        comment_id = result.get("id")
        if isinstance(comment_id, int):
            record_github_note_claim(
                repo=repo,
                target_number=pr_iid,
                comment_id=comment_id,
                body=body,
                target_url=str(result.get("html_url") or ""),
            )
        return result

    def update_comment(self, *, repo: str, comment_id: int, body: str) -> RawAPIDict:
        data = _gh_api_patch(f"repos/{repo}/issues/comments/{comment_id}", {"body": body}, token=self._token)
        return cast("RawAPIDict", data) if isinstance(data, dict) else {}

    def find_review(self, *, repo: str, pr_iid: int, marker: str) -> bool:
        reviews = _gh_api_get_paginated(f"repos/{repo}/pulls/{pr_iid}/reviews?per_page=100", token=self._token)
        return any(marker in str(review.get("body") or "") for review in reviews)

    def submit_review(self, *, repo: str, pr_iid: int, review: PrReview) -> RawAPIDict:
        payload: RawAPIDict = {
            "commit_id": review.commit_sha,
            "body": "\n\n".join(part for part in (review.body, review.marker) if part),
            "event": "COMMENT",
            "comments": [
                {"path": item.path, "line": item.line, "side": "RIGHT", "body": item.body} for item in review.comments
            ],
        }
        data = _gh_api_post(f"repos/{repo}/pulls/{pr_iid}/reviews", payload, token=self._token)
        return cast("RawAPIDict", data) if isinstance(data, dict) else {}

    def approve(self, *, repo: str, pr_iid: int) -> RawAPIDict:
        data = _gh_api_post(f"repos/{repo}/pulls/{pr_iid}/reviews", {"event": "APPROVE"}, token=self._token)
        return cast("RawAPIDict", data) if isinstance(data, dict) else {}
