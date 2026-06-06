"""``ReviewerPrsScanner`` never enqueues — and self-heals — reviewing tasks for self-authored MRs (#1321).

Recurrence the gate forecloses (observed repeatedly): every ``t3 loop tick``
auto-enqueued ``Task(phase="reviewing")`` rows for MRs the user *authored*,
including MRs whose author matched a SECONDARY self-identity (a user owns a
gitlab username as their primary alias plus one or more github logins as
secondary aliases). Self-review via ``t3:reviewer`` is wrong — own MRs route
to coder/debugger + a colleague review-request, never a reviewer sub-agent.

Two structural gaps these tests pin.

Gap one — under-filtering across identities. ``scan()`` fed only the
primary reviewer identity to the skip-condition predicate, so an MR
authored under a non-primary self-identity slipped through as a colleague
MR and emitted ``reviewer_pr.unreviewed`` (a reviewing task was created).

Gap two — no reconciliation of EXISTING self-authored reviewing tasks. A
reviewing ``Task`` already created for a self-authored OPEN MR lingered
forever (the orphan sweep only reaped MERGED/CLOSED PRs), re-surfacing on
every ``pending-spawn``. The scanner now emits a reconciliation signal so
the queue self-heals on the next tick.

These tests drive the real scanner against real ``Ticket``/``Task`` rows and
the mechanical handler, per the teatree integration test doctrine.
"""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket, schedule_external_review
from teatree.loop.dispatch import dispatch
from teatree.loop.mechanical import HANDLERS
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.types import RawAPIDict


@dataclass
class FakeCodeHost:
    """In-memory ``CodeHostBackend`` matching the protocol used by the scanner."""

    user: str = ""
    review_requested_by_reviewer: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    pr_open_state_by_url: dict[str, PrOpenState] = field(default_factory=dict)
    pr_open_state_default: PrOpenState = PrOpenState.UNKNOWN

    def current_user(self) -> str:
        return self.user

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (author, updated_after)
        return []

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = updated_after
        return list(self.review_requested_by_reviewer.get(reviewer, ()))

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        _ = assignee
        return []

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = (pr_url, reviewer)
        return ReviewState.NONE

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


_IDENTITIES = ("user-gl", "user-gh-a", "user-gh-b")


class TestSelfAuthoredAcrossIdentities(TestCase):
    def test_secondary_identity_authored_mr_emits_no_reviewer_signal(self) -> None:
        """An MR authored under a SECONDARY self-identity must not enqueue review.

        Primary identity is ``user-gl``; the MR author is ``user-gh-a``
        (a configured alias). A primary-only filter lets it through as a
        colleague MR — the multi-identity gate must drop it.
        """
        url = "https://github.com/o/r/pull/200"
        host = FakeCodeHost(
            user="user-gl",
            review_requested_by_reviewer={
                "user-gl": [
                    {"html_url": url, "sha": "abc", "user": {"login": "user-gh-a"}, "state": "open"},
                ],
            },
        )
        scanner = ReviewerPrsScanner(host=host, identities=_IDENTITIES)
        signals = scanner.scan()
        review_signals = [s for s in signals if s.kind.startswith("reviewer_pr.") and "task" not in s.kind]
        assert review_signals == [], f"self-authored MR must not emit a review signal; got {review_signals!r}"

    def test_colleague_mr_still_enqueues(self) -> None:
        """Do not over-exclude: a genuine colleague MR still emits ``unreviewed``."""
        url = "https://github.com/o/r/pull/201"
        host = FakeCodeHost(
            user="user-gl",
            review_requested_by_reviewer={
                "user-gl": [
                    {"html_url": url, "sha": "abc", "user": {"login": "bob"}, "state": "open"},
                ],
            },
        )
        scanner = ReviewerPrsScanner(host=host, identities=_IDENTITIES)
        signals = scanner.scan()
        assert [s.kind for s in signals] == ["reviewer_pr.unreviewed"]


class TestReconcileExistingSelfAuthoredReviewingTask(TestCase):
    def _seed_open_reviewing_task(self, url: str, overlay: str = "") -> tuple[Ticket, Task]:
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER, overlay=overlay)
        task = schedule_external_review(ticket)
        assert task.status == Task.Status.PENDING
        return ticket, task

    def test_existing_self_authored_reviewing_task_is_reconciled(self) -> None:
        """An existing PENDING reviewing task on a self-authored OPEN MR self-heals.

        Pre-fix: the orphan sweep only reaped MERGED/CLOSED PRs, so a
        reviewing task created (before the filter landed, or by another path)
        for a self-authored OPEN MR lingered forever. The scanner now emits a
        reconciliation signal; the mechanical handler closes the task.
        """
        url = "https://gitlab/x/-/merge_requests/300"
        _ticket, task = self._seed_open_reviewing_task(url)
        host = FakeCodeHost(
            user="user-gl",
            review_requested_by_reviewer={
                "user-gl": [
                    {"web_url": url, "sha": "abc", "author": {"username": "user-gl"}, "state": "opened"},
                ],
            },
        )
        scanner = ReviewerPrsScanner(host=host, identities=_IDENTITIES)
        signals = scanner.scan()

        reconcile = [s for s in signals if s.kind == "reviewer_pr.task_self_authored"]
        assert reconcile, f"expected a self-authored reconciliation signal; got {[s.kind for s in signals]!r}"

        # Driving the reconciliation signal through dispatch + the mechanical
        # handler must close the lingering task (queue self-heals on next tick).
        actions = dispatch(reconcile)
        mechanical = [a for a in actions if a.kind == "mechanical"]
        assert mechanical, f"reconciliation signal must route to a mechanical handler; got {actions!r}"
        HANDLERS[mechanical[0].zone](mechanical[0].payload)
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_colleague_reviewing_task_is_not_reconciled(self) -> None:
        """A colleague-authored MR's reviewing task is left intact (don't over-close)."""
        url = "https://gitlab/x/-/merge_requests/301"
        _ticket, task = self._seed_open_reviewing_task(url)
        host = FakeCodeHost(
            user="user-gl",
            review_requested_by_reviewer={
                "user-gl": [
                    {"web_url": url, "sha": "abc", "author": {"username": "bob"}, "state": "opened"},
                ],
            },
        )
        scanner = ReviewerPrsScanner(host=host, identities=_IDENTITIES)
        signals = scanner.scan()
        assert [s.kind for s in signals if s.kind == "reviewer_pr.task_self_authored"] == []
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
