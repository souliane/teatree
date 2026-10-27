"""A self-review HOLD is reworked only while its reviewed head is the ticket worktree's live head (#5450)."""

import tempfile
from pathlib import Path

from django.test import TestCase

from teatree.core.modelkit.task_failure_taxonomy import HEAD_SUPERSEDED_PREFIX
from teatree.core.models import Session, Task, Ticket
from teatree.core.models.self_review import SelfReview
from teatree.core.models.task_phase_disposition import (
    advance_self_reviewed_ticket,
    advance_shipped_ticket,
    queue_self_review_rework,
)
from tests._git_repo import make_git_repo, run_git
from tests.factories import WorktreeFactory
from tests.teatree_core._self_review_helpers import author_ticket, completed_self_review, head


def _ticket_with_worktree_at_head(tmp_path: Path) -> tuple[Ticket, str]:
    repo = make_git_repo(tmp_path / "wt")
    live_head = run_git(repo, "rev-parse", "HEAD")
    ticket = author_ticket()
    WorktreeFactory(ticket=ticket, extra={"worktree_path": str(repo)})
    return ticket, live_head


def _rework_for(ticket: Ticket, held: Task, *, status: str = Task.Status.PENDING) -> Task:
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    return Task.objects.create(ticket=ticket, session=session, phase="coding", parent_task=held, status=status)


def _hold(ticket: Ticket) -> SelfReview:
    review = SelfReview.open_hold_for(ticket)
    assert review is not None
    return review


class TestHoldReworkTargetsTheLiveHead(TestCase):
    def setUp(self) -> None:
        self.tmp_path = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_hold_on_a_superseded_head_mints_no_rework(self) -> None:
        ticket, _live = _ticket_with_worktree_at_head(self.tmp_path)
        completed_self_review(ticket, "hold", reviewed_sha=head(1))

        queue_self_review_rework(ticket, _hold(ticket))

        assert not Task.objects.filter(ticket=ticket, phase="coding").exists()

    def test_a_pending_rework_of_a_superseded_head_is_failed_with_the_superseded_reason(self) -> None:
        ticket, _live = _ticket_with_worktree_at_head(self.tmp_path)
        held = completed_self_review(ticket, "hold", reviewed_sha=head(1))
        pending = _rework_for(ticket, held)
        claimed = _rework_for(ticket, held, status=Task.Status.CLAIMED)

        queue_self_review_rework(ticket, _hold(ticket))

        pending.refresh_from_db()
        claimed.refresh_from_db()
        assert pending.status == Task.Status.FAILED
        assert pending.failure_reason.startswith(HEAD_SUPERSEDED_PREFIX)
        assert claimed.status == Task.Status.CLAIMED

    def test_a_hold_on_the_live_head_still_mints_exactly_one_rework(self) -> None:
        ticket, live = _ticket_with_worktree_at_head(self.tmp_path)
        held = completed_self_review(ticket, "hold", reviewed_sha=live)

        queue_self_review_rework(ticket, _hold(ticket))
        queue_self_review_rework(ticket, _hold(ticket))

        [rework] = Task.objects.filter(ticket=ticket, phase="coding")
        assert rework.parent_task_id == held.pk
        assert rework.status == Task.Status.PENDING

    def test_addressed_review_clears_old_shipping_hold_before_next_review(self) -> None:
        ticket = author_ticket(state=Ticket.State.SELF_REVIEWED)
        completed_self_review(ticket, "hold")
        ticket.merge_extra(
            set_keys={
                "shipping_hold": {"reason": "awaiting evidence", "skipped": "no diff", "held_at": "", "checked_at": ""}
            }
        )

        ticket.address_self_review()
        ticket.save()
        ticket.refresh_from_db()

        assert ticket.state == Ticket.State.CODED
        assert "shipping_hold" not in ticket.extra
        assert ticket.schedule_shipping().phase == "shipping"

    def test_an_abbreviated_reviewed_sha_matches_the_live_head_by_prefix(self) -> None:
        ticket, live = _ticket_with_worktree_at_head(self.tmp_path)
        completed_self_review(ticket, "hold", reviewed_sha=live[:12])

        queue_self_review_rework(ticket, _hold(ticket))

        assert Task.objects.filter(ticket=ticket, phase="coding", status=Task.Status.PENDING).count() == 1

    def test_a_nonhex_review_head_proves_no_supersession(self) -> None:
        ticket, _live = _ticket_with_worktree_at_head(self.tmp_path)
        completed_self_review(ticket, "hold", reviewed_sha="head")

        queue_self_review_rework(ticket, _hold(ticket))

        assert Task.objects.filter(ticket=ticket, phase="coding", status=Task.Status.PENDING).count() == 1

    def test_a_stale_hold_at_review_completion_schedules_testing_of_the_live_head(self) -> None:
        ticket, _live = _ticket_with_worktree_at_head(self.tmp_path)
        task = completed_self_review(ticket, "hold", reviewed_sha=head(1))

        advanced = advance_self_reviewed_ticket(task, ticket)

        ticket.refresh_from_db()
        assert advanced is False
        assert ticket.state == Ticket.State.CODED
        assert Task.objects.filter(ticket=ticket, phase="testing", status=Task.Status.PENDING).count() == 1

    def test_a_stale_hold_at_shipping_completion_schedules_testing_of_the_live_head(self) -> None:
        ticket, _live = _ticket_with_worktree_at_head(self.tmp_path)
        ticket.state = Ticket.State.SELF_REVIEWED
        ticket.save(update_fields=["state"])
        completed_self_review(ticket, "hold", reviewed_sha=head(1))
        session = Session.objects.create(ticket=ticket, agent_id="shipping")
        shipping = Task.objects.create(ticket=ticket, session=session, phase="shipping", status=Task.Status.COMPLETED)

        advanced = advance_shipped_ticket(shipping, ticket)

        ticket.refresh_from_db()
        assert advanced is False
        assert ticket.state == Ticket.State.CODED
        assert Task.objects.filter(ticket=ticket, phase="testing", status=Task.Status.PENDING).count() == 1

    def test_a_coding_completion_on_a_superseded_head_re_enters_testing_instead_of_stranding_the_ticket(self) -> None:
        ticket, _live = _ticket_with_worktree_at_head(self.tmp_path)
        held = completed_self_review(ticket, "hold", reviewed_sha=head(1))
        session = Session.objects.create(ticket=ticket, agent_id="coding")
        hand_fix = Task.objects.create(ticket=ticket, session=session, phase="coding")
        hand_fix.claim(claimed_by="coder")

        hand_fix.complete()

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.CODED
        assert Task.objects.filter(ticket=ticket, phase="testing", status=Task.Status.PENDING).count() == 1
        assert not Task.objects.filter(ticket=ticket, phase="coding", parent_task=held).exists()

    def test_an_unknown_worktree_head_keeps_the_rework_behaviour(self) -> None:
        ticket = author_ticket()
        completed_self_review(ticket, "hold", reviewed_sha=head(1))

        queue_self_review_rework(ticket, _hold(ticket))

        assert Task.objects.filter(ticket=ticket, phase="coding", status=Task.Status.PENDING).count() == 1
