"""The #1321 self-authored sweep must not reap a #68 auto-review-armed task (#3910).

Deadlock observed on a solo overlay with ``autonomy=full``: no PR merged for a day
because two loops cancelled each other on every own, CI-green PR.

- ``ship`` / ``PrSweepScanner._flag_no_review`` arms exactly ONE claimable
    ``Task(phase=reviewing)`` per head via ``AutoReviewDispatch.enqueue`` — on a solo
    overlay that is the only path by which an own PR can ever merge, because
    maker≠checker forbids the self-merge.
- ``review`` / ``ReviewerPrsScanner._self_authored_reconcile_signals`` saw the same task
    as a #1321 stray (reviewer-role ticket, non-terminal reviewing task, self-authored MR)
    and completed it within 5 minutes, before any attempt ran.

``AutoReviewDispatch.enqueue`` dedups per ``(slug, pr_id, head_sha)``, so the re-arm on
every later tick returns ``None`` and the PR sits at ``no_independent_review`` for that
head forever. Measured: tasks 683/684/685 completed with ZERO ``TaskAttempt`` rows and no
``ReviewVerdict``, while task 682 won the race and produced a ``merge_safe`` verdict.

The armed task carries its own evidence — an ``AutoReviewDispatch`` row through the
``auto_review_dispatches`` reverse accessor — so the sweep can tell a deliberately armed
review from a genuine stray.
"""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.core.models.auto_review_dispatch import AutoReviewDispatch
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.models.ticket_external_review import schedule_external_review
from teatree.loop.mechanical import reviewer_task_orphaned, reviewer_task_self_authored
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.types import RawAPIDict

_IDENTITIES = ("user-gl", "user-gh-a", "user-gh-b")
_SLUG = "souliane/teatree"
_HEAD = "bd43b8d1c0ffee0000000000000000000000dead"


@dataclass
class FakeCodeHost:
    """In-memory ``CodeHostBackend`` — mirrors the #1321 fake, scoped to this gate."""

    user: str = ""
    review_requested_by_reviewer: dict[str, list[RawAPIDict]] = field(default_factory=dict)
    pr_open_state_default: PrOpenState = PrOpenState.OPEN

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
        _ = pr_url
        return self.pr_open_state_default

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


def _self_authored_host(url: str) -> FakeCodeHost:
    return FakeCodeHost(
        user="user-gl",
        review_requested_by_reviewer={
            "user-gl": [
                {"web_url": url, "sha": _HEAD, "author": {"username": "user-gl"}, "state": "opened"},
            ],
        },
    )


def _arm_auto_review(pr_id: int) -> tuple[str, Ticket, Task]:
    """Arm a review exactly as ``PrSweepScanner._flag_no_review`` does — real rows, no stubs."""
    url = f"https://github.com/{_SLUG}/pull/{pr_id}"
    dispatch = AutoReviewDispatch.enqueue(slug=_SLUG, pr_id=pr_id, head_sha=_HEAD, pr_url=url)
    assert dispatch is not None, "enqueue must arm on first dispatch for this head"
    task = dispatch.task
    assert task is not None
    return url, task.ticket, task


class TestArmedReviewSurvivesSelfAuthoredSweep(TestCase):
    def test_armed_reviewing_task_emits_no_reconcile_signal(self) -> None:
        """The scanner must not flag the ship loop's armed review as a #1321 stray."""
        url, _ticket, task = _arm_auto_review(3894)

        signals = ReviewerPrsScanner(host=_self_authored_host(url), identities=_IDENTITIES).scan()

        reconcile = [s for s in signals if s.kind == "reviewer_pr.task_self_authored"]
        assert reconcile == [], f"armed auto-review must not be reconciled as a stray; got {reconcile!r}"
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_armed_reviewing_task_survives_the_mechanical_handler(self) -> None:
        """Defence in depth: the reaper itself must skip an armed task, whoever emitted the signal."""
        url, ticket, task = _arm_auto_review(3893)

        reviewer_task_self_authored({"ticket_id": ticket.pk, "url": url})

        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_unarmed_stray_is_still_reaped(self) -> None:
        """#1321 non-regression — a reviewing task with no dispatch row is still a stray."""
        url = "https://github.com/souliane/teatree/pull/3800"
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER)
        stray = schedule_external_review(ticket)

        signals = ReviewerPrsScanner(host=_self_authored_host(url), identities=_IDENTITIES).scan()
        assert [s.kind for s in signals if s.kind == "reviewer_pr.task_self_authored"], (
            f"an unarmed self-authored reviewing task must still reconcile; got {[s.kind for s in signals]!r}"
        )

        reviewer_task_self_authored({"ticket_id": ticket.pk, "url": url})
        stray.refresh_from_db()
        assert stray.status == Task.Status.COMPLETED

    def test_mixed_ticket_reaps_only_the_stray(self) -> None:
        """Granularity is per task, not per ticket — the stray goes, the armed review stays."""
        url, ticket, armed = _arm_auto_review(3887)
        stray = schedule_external_review(ticket)

        reviewer_task_self_authored({"ticket_id": ticket.pk, "url": url})

        armed.refresh_from_db()
        stray.refresh_from_db()
        assert armed.status == Task.Status.PENDING
        assert stray.status == Task.Status.COMPLETED

    def test_orphaned_handler_still_reaps_an_armed_task(self) -> None:
        """Behaviour preservation (#998/#1074): once the PR is merged/closed the armed review IS dead work."""
        url, ticket, armed = _arm_auto_review(3899)

        reviewer_task_orphaned({"ticket_id": ticket.pk, "url": url})

        armed.refresh_from_db()
        assert armed.status == Task.Status.COMPLETED


class TestOrphanReasonIsReportedHonestly(TestCase):
    """The orphan sweep has two reap grounds; the log must not report the wrong one (#3910).

    Observed: ``reviewer_task_orphaned`` logged PR 3895 as "confirmed merged/closed" for a
    PR that is in fact OPEN. The #1431 branch reaps on terminal-LOCAL-FSM proof — a
    self-authored MR the user already concluded on legitimately stays open — but the
    handler credited the #1074 forge-state branch either way, so a correct reap read as a
    forge-state bug and sent the operator hunting a phantom.
    """

    def test_terminal_ticket_orphan_signal_carries_its_own_reason(self) -> None:
        url = "https://github.com/souliane/teatree/pull/3895"
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER, state=Ticket.State.REVIEW_POSTED)
        schedule_external_review(ticket)
        assert ticket.is_terminal

        host = FakeCodeHost(user="user-gl", pr_open_state_default=PrOpenState.OPEN)
        signals = ReviewerPrsScanner(host=host, identities=_IDENTITIES).scan()

        orphan = [s for s in signals if s.kind == "reviewer_pr.task_orphaned" and s.payload.get("url") == url]
        assert orphan, f"a terminal reviewer ticket must still be reaped; got {[s.kind for s in signals]!r}"
        assert orphan[0].payload.get("reason") == f"ticket terminal: {ticket.state}"

    def test_merged_pr_orphan_signal_carries_the_forge_reason(self) -> None:
        url = "https://github.com/souliane/teatree/pull/3896"
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER)
        schedule_external_review(ticket)

        host = FakeCodeHost(user="user-gl", pr_open_state_default=PrOpenState.MERGED)
        signals = ReviewerPrsScanner(host=host, identities=_IDENTITIES).scan()

        orphan = [s for s in signals if s.kind == "reviewer_pr.task_orphaned" and s.payload.get("url") == url]
        assert orphan, f"a merged PR's reviewing task must be reaped; got {[s.kind for s in signals]!r}"
        assert orphan[0].payload.get("reason") == "PR merged"

    def test_handler_logs_the_signal_reason_not_a_hardcoded_one(self) -> None:
        url = "https://github.com/souliane/teatree/pull/3897"
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER, state=Ticket.State.REVIEW_POSTED)
        task = schedule_external_review(ticket)

        with self.assertLogs("teatree.loop.mechanical", level="INFO") as captured:
            reviewer_task_orphaned({"ticket_id": ticket.pk, "url": url, "reason": "ticket terminal: review_posted"})

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        logged = "\n".join(captured.output)
        assert "ticket terminal: review_posted" in logged
        assert "merged/closed" not in logged
