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
from teatree.core.models.auto_review_dispatch import AutoReviewDispatch
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.models.ticket_external_review import schedule_external_review
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


class TestSoloOverlayArmedReviewIsNotReaped(TestCase):
    """The #1321 reconcile must not reap the review the ship loop armed (#3910).

    On a solo overlay the ship loop's ``pr_sweep`` is the ONLY path by which
    the operator's own PR can merge: ``_evaluate_solo_overlay`` refuses to
    self-merge, arms exactly one claimable reviewing task, and waits for its
    ``merge_safe`` verdict. That task sits on a reviewer-role ticket whose MR
    is, by construction, self-authored — precisely the shape #1321 reaps.

    The two sweeps therefore cancel: ship arms the review, this scanner
    completes it minutes later, and because ``AutoReviewDispatch`` dedups per
    head the sweep never re-arms it. The PR is stuck at
    ``no_independent_review`` for that head until someone force-pushes.

    The dispatch row is what distinguishes a deliberately-armed review from
    the stray #1321 was written to reap, so it is the discriminator here.
    """

    def test_reviewing_task_with_an_auto_review_dispatch_survives(self) -> None:
        url = "https://gitlab/x/-/merge_requests/302"
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER)
        task = schedule_external_review(ticket)
        AutoReviewDispatch.objects.create(
            slug="x",
            pr_id=302,
            head_sha="abc",
            pr_url=url,
            task=task,
        )
        host = FakeCodeHost(
            user="user-gl",
            review_requested_by_reviewer={
                "user-gl": [
                    {"web_url": url, "sha": "abc", "author": {"username": "user-gl"}, "state": "opened"},
                ],
            },
        )
        scanner = ReviewerPrsScanner(host=host, identities=_IDENTITIES)

        signals = [s for s in scanner.scan() if s.kind == "reviewer_pr.task_self_authored"]

        assert signals == [], (
            "the ship loop's armed cold review was reaped as a self-authored stray — "
            "the own PR can never reach an independent verdict, so it can never merge"
        )
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_stray_self_authored_task_without_a_dispatch_is_still_reaped(self) -> None:
        """The control: #1321's own behaviour is unchanged for a genuine stray."""
        url = "https://gitlab/x/-/merge_requests/303"
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER)
        task = schedule_external_review(ticket)
        host = FakeCodeHost(
            user="user-gl",
            review_requested_by_reviewer={
                "user-gl": [
                    {"web_url": url, "sha": "abc", "author": {"username": "user-gl"}, "state": "opened"},
                ],
            },
        )
        scanner = ReviewerPrsScanner(host=host, identities=_IDENTITIES)

        signals = [s for s in scanner.scan() if s.kind == "reviewer_pr.task_self_authored"]

        assert [s.payload["ticket_id"] for s in signals] == [ticket.pk]
        _ = task


class TestTerminalTicketDoesNotReapArmedReview(TestCase):
    """The #1431 terminal-FSM reap must also spare a deliberately-armed review (#3910).

    This is the branch that actually stalled the factory. ``pr_sweep`` arms its
    cold review on the reviewer-role ticket for the PR — and that ticket is
    routinely ALREADY terminal (``review_posted``) from an earlier review of the
    same PR. ``_orphaned_task_signals`` reaps a non-terminal reviewing task on a
    terminal ticket *regardless of forge state*, so the freshly-armed review died
    on the next tick while the PR was still open and still needed a verdict.

    Forge truth is left fully in charge: a genuinely merged/closed PR still reaps
    its armed review below. Only stale LOCAL FSM state stops being a reason to
    kill work that was deliberately scheduled.
    """

    def _armed(self, url: str, state: str) -> tuple[Ticket, Task]:
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER, state=state)
        task = schedule_external_review(ticket)
        AutoReviewDispatch.objects.create(slug="x", pr_id=400, head_sha="abc", pr_url=url, task=task)
        return ticket, task

    def test_armed_review_survives_alongside_an_unarmed_sibling_task(self) -> None:
        """A ticket is exempt when it carries an armed review, not only when EVERY task is armed.

        The reason this case is spelled out rather than folded into the test
        above: a single-task ticket passes under `.exclude(...__isnull=True)`,
        which compiles to an UNCORRELATED `NOT EXISTS` over every task on the
        ticket in any phase, and so silently means "all tasks are armed". A
        terminal reviewer ticket almost always carries a completed earlier
        review — that is WHY it went terminal — so the single-task shape is the
        rare one and this is the shape the factory actually deadlocked in.
        """
        url = "https://github.com/souliane/teatree/pull/402"
        ticket, task = self._armed(url, Ticket.State.REVIEW_POSTED)
        schedule_external_review(ticket)
        host = FakeCodeHost(user="user-gl", pr_open_state_by_url={url: PrOpenState.OPEN})
        scanner = ReviewerPrsScanner(host=host, identities=_IDENTITIES)

        signals = [s for s in scanner.scan() if s.kind == "reviewer_pr.task_orphaned"]

        assert signals == [], (
            "one ordinary sibling task made the ticket look unarmed, so the terminal branch "
            "reaped the armed review and the open PR is deadlocked again"
        )
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_armed_review_on_a_terminal_ticket_survives_while_the_pr_is_open(self) -> None:
        url = "https://github.com/souliane/teatree/pull/400"
        _ticket, task = self._armed(url, Ticket.State.REVIEW_POSTED)
        host = FakeCodeHost(user="user-gl", pr_open_state_by_url={url: PrOpenState.OPEN})
        scanner = ReviewerPrsScanner(host=host, identities=_IDENTITIES)

        signals = [s for s in scanner.scan() if s.kind == "reviewer_pr.task_orphaned"]

        assert signals == [], (
            "a stale terminal reviewer ticket reaped the cold review the ship loop just armed, "
            "so the open PR can never reach an independent verdict"
        )
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_forge_truth_still_reaps_an_armed_review_on_a_merged_pr(self) -> None:
        """The control: forge state remains authoritative, so real orphans still die."""
        url = "https://github.com/souliane/teatree/pull/401"
        ticket, _task = self._armed(url, Ticket.State.STARTED)
        host = FakeCodeHost(user="user-gl", pr_open_state_by_url={url: PrOpenState.MERGED})
        scanner = ReviewerPrsScanner(host=host, identities=_IDENTITIES)

        signals = [s for s in scanner.scan() if s.kind == "reviewer_pr.task_orphaned"]

        assert [s.payload["ticket_id"] for s in signals] == [ticket.pk]
