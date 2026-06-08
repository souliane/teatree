"""Orphan reviewing tasks on terminal-FSM reviewer tickets self-heal (#1431).

The wedge (#1431): an *orphan reviewing task* is a
``Task(phase="reviewing", status in {PENDING, CLAIMED})`` on a
``Ticket(role=reviewer)`` whose ``state`` is already terminal
(``DELIVERED``/``SHIPPED``/``MERGED``/``IGNORED``). It has no legal FSM
transition left: ``reclaim_orphaned_claims`` flips it back to PENDING, the
loop re-dispatches it, and the reviewer sub-agent's "nothing to post" path
``mark_review_no_action`` raises ``TransitionNotAllowed`` (a terminal state
is not in that transition's source list). The task stays CLAIMED, the lease
expires, and the loop re-dispatches forever.

Two structural gaps close the class — both pinned here.

Gap A — stop the crash at the source. The reviewer's "nothing to post" CLI
path (``mark_review_no_action``) had no terminal state in its FSM
``source=[...]``, so running it on an already-terminal ticket raised
``TransitionNotAllowed``. The transition is now idempotent on a terminal
ticket — a no-op self-transition that consumes the lingering task instead of
crashing the tick. This preserves the #1077 legitimate case (a terminal
ticket whose head SHA moved still gets a fresh review) while closing the
wedge: even if an orphan exists, its disposition can complete cleanly.

Gap B — reap orphans that already slipped through. The orphan sweep
(``_orphaned_task_signals``) only reaped tickets whose forge PR state was
MERGED/CLOSED. A reviewer ticket whose LOCAL FSM is terminal but whose MR
stays OPEN (self-authored, no review owed) was never reaped. The sweep now
also emits ``reviewer_pr.task_orphaned`` when ``ticket.state`` is terminal,
independent of forge state — the local FSM is authoritative for the user's
own decision. The existing fail-open default is preserved: an OPEN MR on a
NON-terminal reviewer ticket still surfaces for review.

These tests drive the real model method, the real persistence entry point,
the real scanner, and the real mechanical handler, per the teatree
integration-test doctrine.
"""

from dataclasses import dataclass, field
from typing import Any

from django.test import TestCase

from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket, schedule_external_review
from teatree.loop.dispatch import dispatch
from teatree.loop.mechanical import HANDLERS
from teatree.loop.persistence import persist_agent_actions
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner, _orphaned_task_signals
from teatree.types import RawAPIDict


@dataclass
class FakeCodeHost:
    """In-memory ``CodeHostBackend`` matching the protocol the scanner uses."""

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


def _seed_open_reviewing_task(ticket: Ticket, *, status: str = Task.Status.PENDING) -> Task:
    """Seed a session-backed open ``phase=reviewing`` task on *ticket*."""
    session = Session.objects.create(ticket=ticket, agent_id="external-review")
    return Task.objects.create(
        ticket=ticket,
        session=session,
        phase="reviewing",
        status=status,
        execution_target=Task.ExecutionTarget.HEADLESS,
    )


class TestGapAMarkReviewNoActionIdempotentOnTerminal(TestCase):
    """Gap A — the no-action disposition never crashes on an already-terminal ticket."""

    def test_mark_review_no_action_is_noop_on_terminal_ticket(self) -> None:
        """An already-terminal reviewer ticket's no-action path is a safe no-op.

        This is the wedge's actual crash point: an orphan reviewing task on a
        terminal ticket is re-dispatched, the reviewer concludes "nothing to
        post", and the ``mark_review_no_action`` CLI path runs. On origin/main
        that raises ``TransitionNotAllowed`` (no terminal state in its
        ``source=[...]``), the task never reaches a terminal state, and the
        loop re-dispatches forever.

        With the fix the transition is idempotent on a terminal ticket: it
        consumes the lingering reviewing task and leaves the state terminal.

        RED on origin/main: ``mark_review_no_action`` raises
        ``TransitionNotAllowed``.
        """
        ticket = Ticket.objects.create(
            issue_url="https://gitlab/x/-/merge_requests/400",
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.DELIVERED,
        )
        orphan = _seed_open_reviewing_task(ticket)

        # On origin/main this raises TransitionNotAllowed (the wedge crash);
        # with the fix it is a no-op that consumes the orphan.
        ticket.mark_review_no_action()
        ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED, "the no-action path must keep a terminal ticket terminal"
        orphan.refresh_from_db()
        assert orphan.status in {
            Task.Status.COMPLETED,
            Task.Status.FAILED,
        }, "the no-action path must consume the lingering reviewing task"

    def test_mark_review_no_action_still_delivers_a_non_terminal_ticket(self) -> None:
        """must-preserve: the original non-terminal disposition still drives the ticket to DELIVERED.

        Guards against over-broadening the transition: a fresh reviewer ticket
        (non-terminal) concluding no-action must still transition to DELIVERED
        and stamp ``REVIEWED_NO_ACTION`` (the #1077 behaviour).
        """
        url = "https://gitlab/x/-/merge_requests/401"
        ticket = Ticket.objects.create(
            issue_url=url,
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.STARTED,
            extra={"reviewed_sha": "sha1"},
        )
        _seed_open_reviewing_task(ticket)

        ticket.mark_review_no_action()
        ticket.save()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        assert ticket.extra.get("last_review_state") == ReviewState.REVIEWED_NO_ACTION.value


class TestGapBOrphanSweepTerminalLocalFsm(TestCase):
    """Gap B — the orphan sweep reaps on terminal LOCAL FSM, independent of forge state."""

    def test_terminal_ticket_open_mr_emits_orphaned_signal(self) -> None:
        """A terminal reviewer ticket with an open reviewing task is reaped even when the MR is OPEN.

        The MR is OPEN and absent from the scanned set (self-authored, no
        review owed). The local FSM is terminal (DELIVERED), so the sweep
        emits ``reviewer_pr.task_orphaned`` despite the OPEN forge state.

        RED on origin/main: OPEN is filtered out (``state not in
        {MERGED, CLOSED}``) so the sweep returns no signal.
        """
        url = "https://gitlab/x/-/merge_requests/402"
        ticket = Ticket.objects.create(
            issue_url=url,
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.DELIVERED,
        )
        _seed_open_reviewing_task(ticket)
        host = FakeCodeHost(pr_open_state_by_url={url: PrOpenState.OPEN})

        signals = _orphaned_task_signals(Ticket, scanned_urls=set(), host=host)

        orphaned = [s for s in signals if s.kind == "reviewer_pr.task_orphaned"]
        assert orphaned, f"terminal-FSM reviewer ticket must emit task_orphaned; got {[s.kind for s in signals]!r}"
        assert orphaned[0].payload["ticket_id"] == ticket.pk

    def test_open_mr_non_terminal_ticket_not_reaped(self) -> None:
        """must-NOT-reap: an OPEN MR on a NON-terminal reviewer ticket still surfaces for review.

        Guards the over-reap blast radius. The fail-open default must hold:
        a genuinely OPEN MR whose reviewer ticket is not terminal is a live
        review obligation and must never be reaped on doubt.
        """
        url = "https://gitlab/x/-/merge_requests/403"
        ticket = Ticket.objects.create(
            issue_url=url,
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.STARTED,
        )
        _seed_open_reviewing_task(ticket)
        host = FakeCodeHost(pr_open_state_by_url={url: PrOpenState.OPEN})

        signals = _orphaned_task_signals(Ticket, scanned_urls=set(), host=host)

        assert [s for s in signals if s.kind == "reviewer_pr.task_orphaned"] == []

    def test_unknown_state_non_terminal_ticket_not_reaped(self) -> None:
        """must-NOT-reap: an UNKNOWN forge state on a NON-terminal ticket never reaps.

        UNKNOWN (auth error, network, unparsable URL) on a non-terminal
        ticket is ambiguous — fail open, never reap on doubt.
        """
        url = "https://gitlab/x/-/merge_requests/404"
        ticket = Ticket.objects.create(
            issue_url=url,
            role=Ticket.Role.REVIEWER,
            state=Ticket.State.STARTED,
        )
        _seed_open_reviewing_task(ticket)
        host = FakeCodeHost(pr_open_state_by_url={url: PrOpenState.UNKNOWN})

        signals = _orphaned_task_signals(Ticket, scanned_urls=set(), host=host)

        assert [s for s in signals if s.kind == "reviewer_pr.task_orphaned"] == []


class TestWedgeIntegrationSingleTick(TestCase):
    """Integration — one scan→dispatch→persist/handle tick reaps the orphan and creates no new one."""

    def _delivered_reviewer_ticket_with_orphan(self, url: str) -> tuple[Ticket, Task]:
        """Reviewer ticket driven to DELIVERED carrying a live PENDING orphan task.

        DELIVERED is reached the real way: a first reviewing task completes
        and fires ``mark_reviewed_externally``. A second reviewing task (the
        orphan) is then seeded PENDING on the now-terminal ticket — exactly
        the #1000/#1431 shape: a reviewing task surviving on a ticket that
        already advanced past it.
        """
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER)
        first = schedule_external_review(ticket)
        assert first is not None
        first.complete()
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED
        orphan = _seed_open_reviewing_task(ticket)
        return ticket, orphan

    def _run_one_tick(self, scanner: ReviewerPrsScanner) -> None:
        """Run the loop's scan→dispatch→(persist agent | handle mechanical) pipeline once."""
        signals = scanner.scan()
        actions = dispatch(signals)
        persist_agent_actions([a for a in actions if a.kind == "agent"])
        for action in actions:
            if action.kind == "mechanical":
                HANDLERS[action.zone](action.payload)

    def test_single_tick_reaps_orphan_and_creates_no_new_one(self) -> None:
        """One tick: the orphan is reaped via the terminal-FSM sweep; no new task; ticket stays terminal.

        The MR is OPEN but ABSENT from the forge review-request scan (a
        Slack-review-request / removed-assignment MR the user already
        concluded on, #1074 absence case). Its URL is therefore never in
        ``scanned_urls``, so the terminal ticket is a sweep candidate, AND
        the #1321 self-authored reconcile path never sees it — leaving the
        Gap B terminal-FSM sweep as the ONLY path that can reap the orphan
        (anti-vacuity). Asserts:
        (a) no NEW reviewing task is created (Gap A — scan never enqueues for
            an MR absent from the forge scan),
        (b) the existing orphan is COMPLETED in one tick (Gap B terminal-FSM
            reap — the only reaping path active here),
        (c) the ticket stays terminal.

        Anti-vacuity: on origin/main the orphan stays PENDING after the tick
        (the OPEN MR is filtered by the MERGED/CLOSED-only gate).
        """
        url = "https://gitlab/x/-/merge_requests/405"
        ticket, orphan = self._delivered_reviewer_ticket_with_orphan(url)
        # The MR is absent from list_review_requested_prs (no forge reviewer
        # assignment), but the forge still reports it OPEN.
        host = FakeCodeHost(
            user="user-gl",
            review_requested_by_reviewer={},
            pr_open_state_by_url={url: PrOpenState.OPEN},
        )
        scanner = ReviewerPrsScanner(host=host, identities=("user-gl",))

        self._run_one_tick(scanner)

        reviewing_tasks = Task.objects.filter(ticket=ticket, phase="reviewing")
        # Only the first (completed) review task + the seeded orphan — no new row.
        assert reviewing_tasks.count() == 2, "the scan must not enqueue a NEW reviewing task on a terminal ticket"
        orphan.refresh_from_db()
        assert orphan.status == Task.Status.COMPLETED, "the existing orphan must be reaped in one tick"
        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.DELIVERED, "the ticket must stay terminal after reaping"
