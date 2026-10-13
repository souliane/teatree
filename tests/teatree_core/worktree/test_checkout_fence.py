from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.models import Worktree
from teatree.core.models.task_claim import drive_claim
from teatree.core.worktree.checkout_fence import CheckoutFenceError, verify_task_checkout
from teatree.core.worktree.occupancy import occupancy_holder, occupy_ticket_checkout, task_holder_id
from tests.factories import TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core.models._shared import _init_repo_with_branch


class CheckoutFenceTests(TestCase):
    def setUp(self) -> None:
        super().setUp()
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name) / "checkout"
        _init_repo_with_branch(self.checkout, branch="feature", commits_ahead=0)
        self.ticket = TicketFactory()
        self.task = TaskFactory(ticket=self.ticket, phase="coding")
        self.worktree = WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})

    def test_first_pristine_sight_attests_then_dirty_state_parks(self) -> None:
        with patch(
            "teatree.core.worktree.checkout_fence.read_process_table",
            return_value=ProcessTable(frozenset(), "/proc"),
        ):
            verify_task_checkout(self.task, str(self.checkout))
            self.worktree.refresh_from_db()
            assert self.worktree.get_extra()["attested_state"]
            (self.checkout / "new.txt").write_text("foreign edit")
            with pytest.raises(CheckoutFenceError, match="attested"):
                verify_task_checkout(self.task, str(self.checkout))

    def test_duplicate_row_or_live_process_refuses_checkout(self) -> None:
        WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(self.checkout)})
        with pytest.raises(CheckoutFenceError, match="exactly one"):
            verify_task_checkout(self.task, str(self.checkout))

        Worktree.objects.filter(ticket=self.ticket).order_by("-pk").first().delete()
        with (
            patch(
                "teatree.core.worktree.checkout_fence.read_process_table",
                return_value=ProcessTable(frozenset({self.checkout}), "/proc"),
            ),
            pytest.raises(CheckoutFenceError, match="live process"),
        ):
            verify_task_checkout(self.task, str(self.checkout))

    def test_worker_completion_attests_only_when_its_checkout_is_released(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        with patch(
            "teatree.core.worktree.checkout_fence.read_process_table",
            return_value=ProcessTable(frozenset(), "/proc"),
        ):
            verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        with (
            patch(
                "teatree.core.worktree.occupancy.read_process_table",
                return_value=ProcessTable(frozenset(), "/proc"),
                create=True,
            ),
            occupy_ticket_checkout(
                self.ticket, holder=task_holder_id(self.task), holder_session=self.task.claimed_by_session
            ),
            drive_claim(self.task),
        ):
            (self.checkout / "new.txt").write_text("run output")
            self.task.complete()
            held = occupancy_holder(Worktree.objects.get(pk=self.worktree.pk))
            assert held is not None
            assert Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"] == initial

        finished = Worktree.objects.get(pk=self.worktree.pk)
        assert occupancy_holder(finished) is None
        assert finished.get_extra()["attested_state"] != initial

    def test_survivor_keeps_claim_and_prior_attestation(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        with patch(
            "teatree.core.worktree.checkout_fence.read_process_table",
            return_value=ProcessTable(frozenset(), "/proc"),
        ):
            verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        with (
            patch(
                "teatree.core.worktree.occupancy.read_process_table",
                return_value=ProcessTable(frozenset({self.checkout}), "/proc"),
            ),
            occupy_ticket_checkout(
                self.ticket, holder=task_holder_id(self.task), holder_session=self.task.claimed_by_session
            ),
            drive_claim(self.task),
        ):
            (self.checkout / "new.txt").write_text("run output")
            self.task.complete()

        finished = Worktree.objects.get(pk=self.worktree.pk)
        held = occupancy_holder(finished)
        assert held is not None
        assert held.holder == task_holder_id(self.task)
        assert finished.get_extra()["attested_state"] == initial
