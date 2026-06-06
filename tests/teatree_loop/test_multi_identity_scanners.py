"""Statusline coverage across the user's multiple identities and code hosts (#976).

A user who has more than one identity on a single forge (e.g. a personal GitHub
login plus an org-account login under the same PAT scope) or who authors PRs on
both code hosts must still see every open PR/MR they own in the statusline.

The pre-fix scanner emitted only one query against ``host.current_user()`` so
PRs authored under a sibling alias were silently invisible. The scanners now
accept ``identities=`` and union-query each one. The factory builds one
``OverlayBackends`` host per configured code-host token so an overlay with both
``github_token_*`` and ``gitlab_token_*`` no longer scans only one platform.
"""

from dataclasses import dataclass, field
from typing import Any

from teatree.core.backend_protocols import ReviewState
from teatree.loop.scanners.assigned_issues import AssignedIssuesScanner
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.types import RawAPIDict


@dataclass
class _IdentityAwareFakeHost:
    """Code host that returns DIFFERENT PRs/issues per queried identity.

    The pre-fix scanners called ``host.current_user()`` once and used the
    return value as the only filter — they never saw PRs filed under any
    other alias. This fake records every ``author=`` / ``reviewer=`` /
    ``assignee=`` argument and returns the per-identity payload so a test
    can assert the scanner queried every configured alias.
    """

    user: str = ""
    prs_by_author: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    review_requested_by_reviewer: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    issues_by_assignee: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    queried_authors: list[str] = field(default_factory=list)
    queried_reviewers: list[str] = field(default_factory=list)
    queried_assignees: list[str] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = updated_after
        self.queried_authors.append(author)
        return list(self.prs_by_author.get(author, ()))

    def list_review_requested_prs(
        self,
        *,
        reviewer: str,
        updated_after: str | None = None,
    ) -> list[RawAPIDict]:
        _ = updated_after
        self.queried_reviewers.append(reviewer)
        return list(self.review_requested_by_reviewer.get(reviewer, ()))

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        self.queried_assignees.append(assignee)
        return list(self.issues_by_assignee.get(assignee, ()))

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = (pr_url, reviewer)
        return ReviewState.NONE

    def create_pr(self, spec: Any) -> RawAPIDict:
        _ = spec
        return {}

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, body)
        return {}

    def update_pr_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> RawAPIDict:
        _ = (repo, pr_iid, comment_id, body)
        return {}

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        _ = (repo, pr_iid)
        return []

    def upload_file(self, *, repo: str, filepath: str) -> RawAPIDict:
        _ = (repo, filepath)
        return {}

    def get_issue(self, issue_url: str) -> RawAPIDict:
        _ = issue_url
        return {}

    def post_issue_comment(self, *, issue_url: str, body: str) -> RawAPIDict:
        _ = (issue_url, body)
        return {}


class TestMyPrsScannerMultiIdentity:
    def test_queries_every_identity_when_aliases_configured(self) -> None:
        host = _IdentityAwareFakeHost(
            user="user-alt",
            prs_by_author={
                "user-alt": [{"iid": 1, "title": "A", "web_url": "https://x/1"}],
                "user-main": [{"iid": 2, "title": "B", "web_url": "https://x/2"}],
            },
        )
        scanner = MyPrsScanner(host=host, identities=("user-alt", "user-main"))
        signals = scanner.scan()
        urls = sorted(s.payload["url"] for s in signals)
        assert urls == ["https://x/1", "https://x/2"], f"both aliases' PRs must surface; got {urls!r}"
        assert sorted(host.queried_authors) == ["user-alt", "user-main"]

    def test_dedupes_pr_returned_under_multiple_identities(self) -> None:
        # When the forge surfaces the same PR for two queries (e.g. one alias
        # is co-author), the dedup-by-url contract keeps the statusline tidy.
        host = _IdentityAwareFakeHost(
            user="user-alt",
            prs_by_author={
                "user-alt": [{"iid": 1, "title": "Shared", "web_url": "https://x/1"}],
                "user-main": [{"iid": 1, "title": "Shared", "web_url": "https://x/1"}],
            },
        )
        scanner = MyPrsScanner(host=host, identities=("user-alt", "user-main"))
        signals = scanner.scan()
        assert [s.payload["url"] for s in signals] == ["https://x/1"]

    def test_falls_back_to_current_user_when_no_identities_passed(self) -> None:
        host = _IdentityAwareFakeHost(
            user="user-alt",
            prs_by_author={
                "user-alt": [{"iid": 1, "title": "A", "web_url": "https://x/1"}],
                "user-main": [{"iid": 2, "title": "B", "web_url": "https://x/2"}],
            },
        )
        # No explicit identities → behave like the legacy single-user scan.
        scanner = MyPrsScanner(host=host)
        signals = scanner.scan()
        assert [s.payload["url"] for s in signals] == ["https://x/1"]


class TestReviewerPrsScannerMultiIdentity:
    def test_queries_every_identity_when_aliases_configured(self, db: None) -> None:
        _ = db
        host = _IdentityAwareFakeHost(
            user="user-alt",
            review_requested_by_reviewer={
                "user-alt": [{"web_url": "https://gl/r/1", "sha": "a"}],
                "user-main": [{"web_url": "https://gl/r/2", "sha": "b"}],
            },
        )
        scanner = ReviewerPrsScanner(host=host, identities=("user-alt", "user-main"))
        signals = scanner.scan()
        urls = sorted(s.payload["url"] for s in signals)
        assert urls == ["https://gl/r/1", "https://gl/r/2"]
        assert sorted(host.queried_reviewers) == ["user-alt", "user-main"]


class TestAssignedIssuesScannerMultiIdentity:
    def test_queries_every_identity_when_aliases_configured(self, db: None) -> None:
        _ = db
        host = _IdentityAwareFakeHost(
            user="user-alt",
            issues_by_assignee={
                "user-alt": [
                    {"web_url": "https://gl/i/1", "title": "A", "labels": ["ready"]},
                ],
                "user-main": [
                    {"html_url": "https://github.com/o/r/issues/9", "title": "B", "labels": ["ready"]},
                ],
            },
        )
        scanner = AssignedIssuesScanner(
            host=host,
            ready_labels=("ready",),
            identities=("user-alt", "user-main"),
        )
        signals = scanner.scan()
        urls = sorted(s.payload["url"] for s in signals)
        assert urls == ["https://github.com/o/r/issues/9", "https://gl/i/1"]
        assert sorted(host.queried_assignees) == ["user-alt", "user-main"]
