"""``ReviewerPrsScanner`` applies the 4 review-candidate skip-conditions (#1321).

The predicate ``should_review_candidate_reasons`` was originally wired into
``followup.discover_mrs`` — the wrong surface, because ``discover_mrs`` consumes
``host.list_my_prs`` (the user's OWN PRs) and the predicate's first skip is
``author_is_self`` (which filtered every own MR out).

The correct consumer is the colleague-MR review-sweep path: the
:class:`ReviewerPrsScanner` consumes ``host.list_review_requested_prs`` which
returns the colleague MRs where the user is a requested reviewer — exactly
the population the 4 skip-conditions are designed to filter (no point
dispatching ``t3:reviewer`` on an MR the user already approved, already
commented on, or that has merged/closed). This test pins the wiring at the
scanner surface so a future regression to ``discover_mrs`` (or any other
own-MR path) goes RED.
"""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.types import RawAPIDict


@dataclass
class FakeCodeHost:
    """In-memory ``CodeHostBackend`` matching the protocol used by the scanner."""

    user: str = ""
    my_prs: list[RawAPIDict] = field(default_factory=list)
    review_requested_prs: list[RawAPIDict] = field(default_factory=list)
    assigned_issues: list[RawAPIDict] = field(default_factory=list)
    review_state_by_url: dict[str, ReviewState] = field(default_factory=dict)
    pr_open_state_by_url: dict[str, PrOpenState] = field(default_factory=dict)
    pr_open_state_default: PrOpenState = PrOpenState.UNKNOWN

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return self.my_prs

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return self.review_requested_prs

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return self.assigned_issues

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = reviewer
        return self.review_state_by_url.get(pr_url, ReviewState.NONE)

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        return self.pr_open_state_by_url.get(pr_url, self.pr_open_state_default)

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


class TestReviewerPrsScannerSkipConditions(TestCase):
    def test_authored_by_self_is_skipped(self) -> None:
        """A self-authored MR returned by the forge as a review request is filtered.

        ``list_review_requested_prs`` can return a self-authored MR (e.g. the
        forge accidentally added the user as their own reviewer); the predicate
        must drop it before reviewer dispatch.
        """
        url = "https://gitlab/x/-/merge_requests/100"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[
                {
                    "web_url": url,
                    "sha": "abc",
                    "author": {"username": "alice"},
                    "state": "opened",
                },
            ],
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()
        assert signals == []

    def test_already_approved_by_self_is_skipped(self) -> None:
        url = "https://gitlab/x/-/merge_requests/101"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[
                {
                    "web_url": url,
                    "sha": "abc",
                    "author": {"username": "bob"},
                    "state": "opened",
                    "approvers": [{"username": "alice"}],
                },
            ],
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()
        assert signals == []

    def test_has_self_authored_note_is_skipped(self) -> None:
        url = "https://gitlab/x/-/merge_requests/102"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[
                {
                    "web_url": url,
                    "sha": "abc",
                    "author": {"username": "bob"},
                    "state": "opened",
                    "notes": [
                        {"author": {"username": "alice"}, "system": False, "body": "already engaged"},
                    ],
                },
            ],
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()
        assert signals == []

    def test_merged_state_is_skipped(self) -> None:
        url = "https://gitlab/x/-/merge_requests/103"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[
                {
                    "web_url": url,
                    "sha": "abc",
                    "author": {"username": "bob"},
                    "state": "merged",
                },
            ],
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()
        assert signals == []

    def test_closed_state_is_skipped(self) -> None:
        url = "https://gitlab/x/-/merge_requests/104"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[
                {
                    "web_url": url,
                    "sha": "abc",
                    "author": {"username": "bob"},
                    "state": "closed",
                },
            ],
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()
        assert signals == []

    def test_clean_colleague_mr_still_emits_unreviewed(self) -> None:
        """The happy path is preserved: a non-skip MR still emits a review signal."""
        url = "https://gitlab/x/-/merge_requests/105"
        host = FakeCodeHost(
            user="alice",
            review_requested_prs=[
                {
                    "web_url": url,
                    "sha": "abc",
                    "author": {"username": "bob"},
                    "state": "opened",
                },
            ],
        )
        scanner = ReviewerPrsScanner(host=host)
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.unreviewed"]
