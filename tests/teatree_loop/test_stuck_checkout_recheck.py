from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.task import Task
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
        assert self.task.not_before > timezone.now() + timedelta(days=365)
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

    def test_non_checkout_and_superseded_halts_are_left_alone(self) -> None:
        self.ticket.merge_extra(merge_into_dicts={"stuck_halt": {"reason": "budget spent"}})
        assert self.scanner.scan() == []
        self.ticket.merge_extra(merge_into_dicts={"stuck_halt": {"reason": "checkout parked: foreign state"}})
        TaskFactory(ticket=self.ticket, session=SessionFactory(ticket=self.ticket), phase="coding")
        assert self.scanner.scan() == []
        self.task.refresh_from_db()
        assert self.task.not_before > timezone.now() + timedelta(days=365)

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
