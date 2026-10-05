"""The reviewer-PR sweep closes a reviewing task only when no review is owed (#4901).

Observed: task 5144 (PR #4898, head 782ec2a3) was minted on a ``review_delivered``
reviewer ticket whose PR was OPEN and held, and the sweep closed it five minutes
into its run ("lease lost; interrupting duplicate run"). The mint side read the
ticket as owed a review (``review_delivered`` is a ``mark_reviewed_externally``
source); the reap side read the same state as terminal. And the sweep closed a
CLAIMED task, killing a live run.

Every test drives the real scanner, dispatch and ``HANDLERS`` against DB rows.
"""

from dataclasses import dataclass, field

from django.test import TestCase
from django.utils import timezone

from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.core.models.merge_clear import MergeClear
from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.models.ticket_external_review import ReviewDeclined, reviewer_dispatch_decline
from teatree.loop.dispatch import dispatch
from teatree.loop.mechanical import HANDLERS
from teatree.loop.scanners.base import ScanSignal
from teatree.loop.scanners.reviewer_prs import ReviewerPrsScanner
from teatree.types import RawAPIDict
from tests._pr_open_state_stub import mint_open_pr_review, pr_open_state
from tests.teatree_core.conftest import record_review_context_for_test

_SLUG = "souliane/teatree"
_HEAD = "782ec2a3" + "0" * 32
_REAP = "reviewer_pr.task_orphaned"
_SETTLED_ADMITTING_NO_REVIEW = (
    Ticket.State.IGNORED,
    Ticket.State.PR_OPENED,
    Ticket.State.MERGED,
    Ticket.State.DELIVERED,
)


@dataclass
class _ForgeHost:
    pr_open_state_by_url: dict[str, PrOpenState] = field(default_factory=dict)

    def current_user(self) -> str:
        return "user-gh"

    def list_review_requested_prs(self, *, reviewer: str, updated_after: str | None = None) -> list[RawAPIDict]:
        _ = (reviewer, updated_after)
        return []

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        _ = (pr_url, reviewer)
        return ReviewState.NONE

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        return self.pr_open_state_by_url.get(pr_url, PrOpenState.UNKNOWN)


def _url(pr_id: int) -> str:
    return f"https://github.com/{_SLUG}/pull/{pr_id}"


def _tick(host: _ForgeHost) -> list[ScanSignal]:
    signals = ReviewerPrsScanner(host=host, identities=("user-gh",)).scan()
    for action in dispatch(signals):
        if action.kind == "mechanical":
            HANDLERS[action.zone](action.payload)
    return [s for s in signals if s.kind == _REAP]


def _tick_scan_only(host: _ForgeHost) -> list[ScanSignal]:
    return [s for s in ReviewerPrsScanner(host=host, identities=("user-gh",)).scan() if s.kind == _REAP]


def _reviewing_task(ticket: Ticket, *, status: str = Task.Status.PENDING) -> Task:
    session = Session.objects.create(ticket=ticket, agent_id="external-review")
    return Task.objects.create(ticket=ticket, session=session, phase="reviewing", status=status)


def _review_delivered_ticket(pr_id: int) -> Ticket:
    """A reviewer ticket that reached ``review_delivered`` the real way: a first review completed."""
    ticket = Ticket.objects.create(issue_url=_url(pr_id), role=Ticket.Role.REVIEWER)
    record_review_context_for_test(ticket)
    mint_open_pr_review(ticket).complete()
    ticket.refresh_from_db()
    assert ticket.state == Ticket.State.REVIEW_DELIVERED
    return ticket


def _ticket_in(state: str, pr_id: int) -> tuple[Ticket, Task]:
    ticket = Ticket.objects.create(issue_url=_url(pr_id), role=Ticket.Role.REVIEWER)
    task = _reviewing_task(ticket)
    Ticket.objects.filter(pk=ticket.pk).update(state=state)
    ticket.refresh_from_db()
    return ticket, task


class TestAReviewOwedOnAnOpenPrIsNeverReaped(TestCase):
    def test_a_claimed_review_on_an_open_review_delivered_pr_keeps_running(self) -> None:
        url = _url(4898)
        ticket = _review_delivered_ticket(4898)
        task = mint_open_pr_review(ticket)
        task.claim(claimed_by="worker-a")

        reaped = _tick(_ForgeHost({url: PrOpenState.OPEN}))

        assert reaped == [], "a review_delivered ticket with an open PR is owed a review"
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED, "the sweep killed a running review"
        assert task.attempts.count() == 0

    def test_a_held_head_on_an_open_pr_is_not_reaped_until_the_pr_merges(self) -> None:
        url = _url(4897)
        ticket = _review_delivered_ticket(4897)
        ReviewVerdict.objects.create(
            pr_id=4897,
            slug=_SLUG,
            reviewed_sha=_HEAD,
            verdict=ReviewVerdict.Verdict.HOLD,
            reviewer_identity="cold-reviewer",
            blast_class=MergeClear.BlastClass.LOGIC,
            gh_verify_result=MergeClear.VerifyResult.GREEN,
        )
        task = mint_open_pr_review(ticket)
        host = _ForgeHost({url: PrOpenState.OPEN})

        assert _tick(host) == []
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

        host.pr_open_state_by_url[url] = PrOpenState.MERGED
        reaped = _tick(host)

        assert [s.payload["reason"] for s in reaped] == ["PR merged"]
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestOnlyAPendingTaskIsClosed(TestCase):
    def test_a_merged_pr_closes_the_pending_sibling_and_leaves_the_claimed_run(self) -> None:
        url = _url(4901)
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER, state=Ticket.State.WORK_STARTED)
        running = _reviewing_task(ticket)
        running.claim(claimed_by="worker-a")
        pending = _reviewing_task(ticket)

        reaped = _tick(_ForgeHost({url: PrOpenState.MERGED}))

        assert [s.payload["ticket_id"] for s in reaped] == [ticket.pk]
        pending.refresh_from_db()
        running.refresh_from_db()
        assert pending.status == Task.Status.COMPLETED
        assert running.status == Task.Status.CLAIMED
        assert running.attempts.count() == 0

    def test_a_dead_claim_on_a_merged_pr_is_closed_once_reclaim_requeues_it(self) -> None:
        url = _url(4902)
        ticket = Ticket.objects.create(issue_url=url, role=Ticket.Role.REVIEWER, state=Ticket.State.WORK_STARTED)
        past = timezone.now() - timezone.timedelta(minutes=10)
        task = _reviewing_task(ticket, status=Task.Status.CLAIMED)
        Task.objects.filter(pk=task.pk).update(claimed_by="dead-worker", claimed_at=past, lease_expires_at=past)
        host = _ForgeHost({url: PrOpenState.MERGED})

        assert _tick(host) == []
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED

        assert Task.objects.reclaim_orphaned_claims() == 1
        reaped = _tick(host)

        assert [s.payload["reason"] for s in reaped] == ["PR merged"]
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestTheReapAndTheMintAgree(TestCase):
    def test_a_ticket_reaped_on_an_open_pr_is_one_the_mint_owes_nothing(self) -> None:
        reaped_states = []
        for pr_id, state in enumerate(Ticket.State.values, start=5000):
            with self.subTest(state=state):
                ticket, _task = _ticket_in(state, pr_id)
                reaped = [
                    s
                    for s in _tick_scan_only(_ForgeHost({ticket.issue_url: PrOpenState.OPEN}))
                    if s.payload["ticket_id"] == ticket.pk
                ]
                with pr_open_state(PrOpenState.OPEN):
                    decline = reviewer_dispatch_decline(ticket)

                assert not (reaped and decline is None), f"{state}: the mint owes a review the reap closes"
                if reaped:
                    assert decline is ReviewDeclined.NOTHING_OWED
                    reaped_states.append(state)

        assert Ticket.State.IGNORED in reaped_states, "the agreement check must see at least one reap"
        assert Ticket.State.REVIEW_DELIVERED not in reaped_states

    def test_review_delivered_is_owed_a_review_and_not_reaped(self) -> None:
        ticket, task = _ticket_in(Ticket.State.REVIEW_DELIVERED, 5100)

        with pr_open_state(PrOpenState.OPEN):
            assert reviewer_dispatch_decline(ticket) is None
        assert _tick(_ForgeHost({ticket.issue_url: PrOpenState.OPEN})) == []
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING


class TestAStateThatAdmitsNoReviewIsStillReaped(TestCase):
    def test_a_pending_task_is_closed_on_an_open_pr(self) -> None:
        for pr_id, state in enumerate(_SETTLED_ADMITTING_NO_REVIEW, start=5200):
            with self.subTest(state=state):
                ticket, task = _ticket_in(state, pr_id)

                reaped = _tick(_ForgeHost({ticket.issue_url: PrOpenState.OPEN}))

                assert [s.payload["reason"] for s in reaped] == [f"ticket {state} admits no review"]
                task.refresh_from_db()
                assert task.status == Task.Status.COMPLETED

    def test_the_reap_names_its_ground_and_never_calls_an_owed_review_orphaned(self) -> None:
        ticket, _task = _ticket_in(Ticket.State.IGNORED, 5300)

        with self.assertLogs("teatree.loop.mechanical", level="INFO") as captured:
            reaped = _tick(_ForgeHost({ticket.issue_url: PrOpenState.OPEN}))

        logged = "\n".join(captured.output)
        assert "ticket ignored admits no review" in logged
        assert "no review owed" in logged
        assert "orphan" not in logged.lower()
        assert "orphan" not in reaped[0].summary.lower()
