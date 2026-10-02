"""URL-prefix gate keeps cross-overlay MRs out of the wrong overlay's statusline.

``MyPrsScanner`` and ``ReviewerPrsScanner`` are registered per
``(overlay x code_host)``. Pre-fix, a scanner emitted every MR it saw on a
host, even when the project URL belonged to a sibling overlay. The bleed
shows up as the same MR rendered under two ``[ov]`` prefixes (#1015).

When ``allowed_url_prefixes`` is configured, the scanner only emits PRs
whose ``web_url`` / ``html_url`` starts with one of the prefixes. An empty
tuple keeps the legacy "emit all" behaviour.
"""

from dataclasses import dataclass, field
from typing import Any

from teatree.core.backend_protocols import ReviewState
from teatree.loop.scanners.my_prs import MyPrsScanner
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.types import RawAPIDict


@dataclass
class _Host:
    user: str = "alice"
    prs: list[RawAPIDict] = field(default_factory=list)
    reviewer_prs: list[RawAPIDict] = field(default_factory=list)

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return list(self.prs)

    def list_review_requested_prs(
        self,
        *,
        reviewer: str,
        updated_after: str | None = None,
    ) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return list(self.reviewer_prs)

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return []

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


class TestMyPrsScannerUrlPrefixGate:
    def test_drops_pr_outside_allowed_prefixes(self) -> None:
        host = _Host(
            prs=[
                {"iid": 1, "title": "A", "web_url": "https://gitlab.com/acme/product/-/merge_requests/1"},
                {"iid": 2, "title": "B", "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/2"},
            ],
        )
        scanner = MyPrsScanner(
            host=host,
            allowed_url_prefixes=("https://gitlab.com/acme/",),
        )
        signals = scanner.scan()
        urls = sorted(s.payload["url"] for s in signals)
        assert urls == ["https://gitlab.com/acme/product/-/merge_requests/1"]

    def test_empty_prefixes_preserves_legacy_behaviour(self) -> None:
        host = _Host(
            prs=[
                {"iid": 1, "title": "A", "web_url": "https://gitlab.com/acme/product/-/merge_requests/1"},
                {"iid": 2, "title": "B", "web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/2"},
            ],
        )
        scanner = MyPrsScanner(host=host, allowed_url_prefixes=())
        urls = sorted(s.payload["url"] for s in scanner.scan())
        assert urls == [
            "https://gitlab.com/acme/product/-/merge_requests/1",
            "https://gitlab.com/souliane/teatree/-/merge_requests/2",
        ]

    def test_multiple_prefixes_each_match(self) -> None:
        host = _Host(
            prs=[
                {"iid": 1, "title": "A", "web_url": "https://gitlab.com/acme/product/-/merge_requests/1"},
                {"iid": 2, "title": "B", "web_url": "https://github.com/acme/repo/pull/9"},
                {"iid": 3, "title": "C", "web_url": "https://gitlab.com/elsewhere/-/merge_requests/3"},
            ],
        )
        scanner = MyPrsScanner(
            host=host,
            allowed_url_prefixes=(
                "https://gitlab.com/acme/",
                "https://github.com/acme/",
            ),
        )
        urls = sorted(s.payload["url"] for s in scanner.scan())
        assert urls == [
            "https://github.com/acme/repo/pull/9",
            "https://gitlab.com/acme/product/-/merge_requests/1",
        ]

    def test_pr_with_no_url_is_dropped_when_prefixes_set(self) -> None:
        host = _Host(prs=[{"iid": 5, "title": "no url"}])
        scanner = MyPrsScanner(host=host, allowed_url_prefixes=("https://gitlab.com/acme/",))
        assert scanner.scan() == []


class TestReviewerPrsScannerUrlPrefixGate:
    def test_drops_review_requested_pr_outside_allowed_prefixes(self, db: None) -> None:
        _ = db
        host = _Host(
            reviewer_prs=[
                {"web_url": "https://gitlab.com/acme/product/-/merge_requests/1", "sha": "a"},
                {"web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/2", "sha": "b"},
            ],
        )
        scanner = ReviewerPrsScanner(
            host=host,
            allowed_url_prefixes=("https://gitlab.com/acme/",),
        )
        urls = sorted(s.payload["url"] for s in scanner.scan())
        assert urls == ["https://gitlab.com/acme/product/-/merge_requests/1"]

    def test_empty_prefixes_preserves_legacy_behaviour(self, db: None) -> None:
        _ = db
        host = _Host(
            reviewer_prs=[
                {"web_url": "https://gitlab.com/acme/product/-/merge_requests/1", "sha": "a"},
                {"web_url": "https://gitlab.com/souliane/teatree/-/merge_requests/2", "sha": "b"},
            ],
        )
        scanner = ReviewerPrsScanner(host=host)
        urls = sorted(s.payload["url"] for s in scanner.scan())
        assert urls == [
            "https://gitlab.com/acme/product/-/merge_requests/1",
            "https://gitlab.com/souliane/teatree/-/merge_requests/2",
        ]
