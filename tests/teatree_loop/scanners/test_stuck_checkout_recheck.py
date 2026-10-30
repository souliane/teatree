from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.session import Session
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.worktree.checkout_fence import CheckoutFenceError, park_task_checkout
from teatree.loop.scanners.stuck_checkout_recheck import StuckCheckoutRecheckScanner, _claim_recheck
from tests.factories import SessionFactory, TaskFactory, TicketFactory, WorktreeFactory


class StuckCheckoutRecheckTests(TestCase):
    def setUp(self) -> None:
        self.ticket = TicketFactory()
        self.task = TaskFactory(ticket=self.ticket, session=SessionFactory(ticket=self.ticket), phase="coding")
        WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(Path.cwd())})
        for _ in range(3):
            park_task_checkout(self.task, reason="foreign checkout state")
        self.scanner = StuckCheckoutRecheckScanner()

    def test_unchanged_cause_keeps_the_park_and_throttles_rechecks(self) -> None:
        with patch(
            "teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout",
            side_effect=CheckoutFenceError("foreign checkout state"),
        ) as verify:
            assert self.scanner.scan() == []
            assert self.scanner.scan() == []
        self.task.refresh_from_db()
        self.ticket.refresh_from_db()
        assert verify.call_count == 1
        assert self.task.not_before > timezone.now() + timedelta(minutes=59)
        assert "stuck_halt" in self.ticket.extra

    def test_changed_cause_rearms_the_task_and_resolves_the_halt(self) -> None:
        with patch("teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout"):
            signals = self.scanner.scan()
        self.task.refresh_from_db()
        self.ticket.refresh_from_db()
        assert len(signals) == 1
        assert self.task.not_before <= timezone.now()
        assert "stuck_halt" not in self.ticket.extra

    def test_new_reviewed_head_is_rechecked_and_rearmed(self) -> None:
        self.ticket.merge_extra(set_keys={"reviewed_sha": "new-head"})
        with patch("teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout"):
            assert len(self.scanner.scan()) == 1
        self.task.refresh_from_db()
        assert self.task.not_before <= timezone.now()

    def test_non_checkout_halt_is_left_alone_and_superseded_checkout_halt_is_cleared(self) -> None:
        self.ticket.merge_extra(merge_into_dicts={"stuck_halt": {"reason": "budget spent"}})
        assert self.scanner.scan() == []
        self.ticket.merge_extra(merge_into_dicts={"stuck_halt": {"reason": "checkout parked: foreign state"}})
        TaskFactory(ticket=self.ticket, session=SessionFactory(ticket=self.ticket), phase="coding")
        assert self.scanner.scan() == []
        self.ticket.refresh_from_db()
        assert "stuck_halt" not in self.ticket.extra
        self.task.refresh_from_db()
        assert self.task.not_before > timezone.now() + timedelta(minutes=59)

    def test_non_writing_task_stays_parked_until_its_checkout_can_be_checked(self) -> None:
        Task.objects.filter(pk=self.task.pk).update(phase="reviewing")
        with patch("teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout") as verify:
            assert self.scanner.scan() == []
        verify.assert_not_called()
        self.ticket.refresh_from_db()
        self.task.refresh_from_db()
        assert "stuck_halt" in self.ticket.extra
        assert self.task.not_before > timezone.now() + timedelta(minutes=59)

    def test_halt_for_a_completed_task_is_cleared(self) -> None:
        Task.objects.filter(pk=self.task.pk).update(status=Task.Status.COMPLETED)
        assert self.scanner.scan() == []
        self.ticket.refresh_from_db()
        assert "stuck_halt" not in self.ticket.extra

    def test_missing_checkout_path_keeps_the_halt(self) -> None:
        self.ticket.worktrees.update(extra={})
        assert self.scanner.scan() == []
        self.ticket.refresh_from_db()
        assert "recheck_at" in self.ticket.extra["stuck_halt"]

    def test_already_due_task_clears_stale_halt_after_fence_passes(self) -> None:
        Task.objects.filter(pk=self.task.pk).update(not_before=timezone.now() - timedelta(seconds=1))
        with patch("teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout"):
            assert len(self.scanner.scan()) == 1
        self.ticket.refresh_from_db()
        assert "stuck_halt" not in self.ticket.extra

    def test_malformed_recheck_stamp_is_replaced_and_removed_stamp_is_not_claimed(self) -> None:
        self.ticket.merge_extra(merge_into_dicts={"stuck_halt": {"recheck_at": "bad-date"}})
        with patch(
            "teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout",
            side_effect=CheckoutFenceError("still blocked"),
        ):
            assert self.scanner.scan() == []
        self.ticket.refresh_from_db()
        assert self.ticket.extra["stuck_halt"]["recheck_at"] != "bad-date"
        self.ticket.merge_extra(pop_keys=["stuck_halt"])
        assert _claim_recheck(self.ticket, timezone.now()) is False

    def test_naive_recheck_stamp_is_replaced(self) -> None:
        self.ticket.merge_extra(merge_into_dicts={"stuck_halt": {"recheck_at": "2020-01-01T00:00:00"}})
        with patch(
            "teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout",
            side_effect=CheckoutFenceError("still blocked"),
        ):
            assert self.scanner.scan() == []
        self.ticket.refresh_from_db()
        assert self.ticket.extra["stuck_halt"]["recheck_at"] != "2020-01-01T00:00:00"

    def test_old_valid_recheck_stamp_allows_another_check(self) -> None:
        self.ticket.merge_extra(merge_into_dicts={"stuck_halt": {"recheck_at": "2020-01-01T00:00:00+00:00"}})
        with patch(
            "teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout",
            side_effect=CheckoutFenceError("still blocked"),
        ) as verify:
            assert self.scanner.scan() == []
        assert verify.call_count == 1

    def test_concurrent_claim_during_fence_probe_does_not_rearm_or_clear_halt(self) -> None:
        def claim_during_probe(task: Task, path: str) -> None:
            Task.objects.filter(pk=task.pk).update(status=Task.Status.CLAIMED)

        with patch("teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout", side_effect=claim_during_probe):
            assert self.scanner.scan() == []
        self.task.refresh_from_db()
        self.ticket.refresh_from_db()
        assert self.task.status == Task.Status.CLAIMED
        assert "stuck_halt" in self.ticket.extra

    def test_non_checkout_halts_do_not_starve_later_checkout_park(self) -> None:
        self.ticket.merge_extra(merge_into_dicts={"stuck_halt": {"reason": "budget spent"}})
        for _ in range(50):
            TicketFactory(extra={"stuck_halt": {"reason": "budget spent"}})
        later = TicketFactory()
        task = TaskFactory(ticket=later, session=SessionFactory(ticket=later), phase="coding")
        WorktreeFactory(ticket=later, extra={"worktree_path": str(Path.cwd())})
        for _ in range(3):
            park_task_checkout(task, reason="foreign checkout state")
        with patch("teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout"):
            signals = self.scanner.scan()
        later.refresh_from_db()
        assert [signal.payload["ticket_id"] for signal in signals] == [later.pk]
        assert "stuck_halt" not in later.extra

    def test_recently_rechecked_halts_do_not_starve_later_checkout_park(self) -> None:
        self.ticket.merge_extra(merge_into_dicts={"stuck_halt": {"recheck_at": timezone.now().isoformat()}})
        tickets = Ticket.objects.bulk_create(TicketFactory.build_batch(49))
        sessions = Session.objects.bulk_create([Session(ticket=ticket, overlay=ticket.overlay) for ticket in tickets])
        tasks = Task.objects.bulk_create(
            [
                Task(ticket=ticket, session=session, phase="coding")
                for ticket, session in zip(tickets, sessions, strict=True)
            ]
        )
        for ticket, task in zip(tickets, tasks, strict=True):
            ticket.extra = {
                "stuck_halt": {
                    "reason": "checkout parked: foreign checkout state",
                    "newest_task": task.pk,
                    "reviewed_sha": "",
                    "recheck_at": timezone.now().isoformat(),
                }
            }
        Ticket.objects.bulk_update(tickets, ["extra"])
        later = TicketFactory()
        task = TaskFactory(ticket=later, session=SessionFactory(ticket=later), phase="coding")
        WorktreeFactory(ticket=later, extra={"worktree_path": str(Path.cwd())})
        for _ in range(3):
            park_task_checkout(task, reason="foreign checkout state")
        with patch("teatree.loop.scanners.stuck_checkout_recheck.verify_task_checkout"):
            signals = self.scanner.scan()
        assert [signal.payload["ticket_id"] for signal in signals] == [later.pk]
