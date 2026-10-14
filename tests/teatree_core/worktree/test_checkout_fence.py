from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.agents import runner
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.models.task_claim import drive_claim
from teatree.core.models.task_repair import check_requeue_allowed, phase_attempts
from teatree.core.worktree.checkout_fence import CheckoutFenceError, park_task_checkout, verify_task_checkout
from teatree.core.worktree.checkout_state import checkout_state
from teatree.core.worktree.occupancy import acquire, occupancy_holder, occupy_ticket_checkout, release, task_holder_id
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

    def test_checkout_parks_do_not_spend_repair_budget(self) -> None:
        for _ in range(5):
            attempt = park_task_checkout(self.task, reason="foreign checkout state")
            assert attempt.error.startswith("limit_parked: checkout_parked:")
        assert phase_attempts(self.task) == []
        check_requeue_allowed(self.task)

    def test_owner_attestation_allows_only_one_claim_without_process_table(self) -> None:
        state = checkout_state(self.checkout)
        self.worktree.merge_extra(set_keys={"attested_state": f"owner:{state.digest}"})
        unavailable = ProcessTable(frozenset(), "", ("host process table unavailable",))
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=unavailable):
            verify_task_checkout(self.task, str(self.checkout))
            with pytest.raises(CheckoutFenceError, match="process table unavailable"):
                verify_task_checkout(self.task, str(self.checkout))

    def test_owner_attestation_remains_unused_when_confirmation_cannot_be_read(self) -> None:
        state = checkout_state(self.checkout)
        attestation = f"owner:{state.digest}"
        self.worktree.merge_extra(set_keys={"attested_state": attestation})
        unavailable = ProcessTable(frozenset(), "", ("host process table unavailable",))
        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=unavailable),
            patch(
                "teatree.core.worktree.checkout_fence.checkout_state", side_effect=[state, RuntimeError("unreadable")]
            ),
            pytest.raises(CheckoutFenceError, match="could not be confirmed"),
        ):
            verify_task_checkout(self.task, str(self.checkout))
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == attestation

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

    def test_cross_ticket_duplicate_row_refuses_checkout(self) -> None:
        WorktreeFactory(ticket=TicketFactory(), extra={"worktree_path": str(self.checkout)})
        with pytest.raises(CheckoutFenceError, match="exactly one"):
            verify_task_checkout(self.task, str(self.checkout))

    def test_second_repository_edit_refuses_whole_ticket(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        other = Path(temporary.name) / "other"
        _init_repo_with_branch(other, branch="feature", commits_ahead=0)
        WorktreeFactory(ticket=self.ticket, extra={"worktree_path": str(other)})
        clear = ProcessTable(frozenset(), "/proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(self.task, str(self.checkout))
            (other / "in_session.py").write_text("foreign edit\n")
            with pytest.raises(CheckoutFenceError, match="attested"):
                verify_task_checkout(self.task, str(self.checkout))

    def test_worker_completion_releases_and_attests_before_handoff(self) -> None:
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
            assert held is None
            assert Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"] != initial

        finished = Worktree.objects.get(pk=self.worktree.pk)
        assert occupancy_holder(finished) is None
        assert finished.get_extra()["attested_state"] != initial

    def test_next_phase_started_inside_completion_uses_released_attestation(self) -> None:
        clear = ProcessTable(frozenset(), "/proc")
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(self.task, str(self.checkout))
        testing = TaskFactory(ticket=self.ticket, phase="testing")
        with (
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=clear),
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear),
            occupy_ticket_checkout(self.ticket, holder=task_holder_id(self.task), holder_session="s1"),
            drive_claim(self.task),
        ):
            (self.checkout / "work.txt").write_text("coding output")
            self.task.complete()
            testing.claim(claimed_by="worker-2", claimed_by_session="s2")
            with patch.object(runner, "_run_agent", return_value=Mock(spec=TaskAttempt)) as drive:
                runner.run_agent(testing, phase="testing", overlay_skill_metadata={})
        drive.assert_called_once()
        assert Task.objects.get(pk=testing.pk).status == Task.Status.CLAIMED

    def test_a_run_whose_checkout_was_handed_to_another_holder_never_attests(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        clear = ProcessTable(frozenset(), "/proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]

        with (
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=clear),
            occupy_ticket_checkout(
                self.ticket, holder=task_holder_id(self.task), holder_session=self.task.claimed_by_session
            ),
            drive_claim(self.task),
        ):
            (self.checkout / "new.txt").write_text("run output")
            handed = Worktree.objects.get(pk=self.worktree.pk)
            assert release(handed, holder=task_holder_id(self.task), holder_session="s1")
            acquire(handed, holder="owner-terminal", holder_session="owner")
            self.task.complete()

        finished = Worktree.objects.get(pk=self.worktree.pk)
        held = occupancy_holder(finished)
        assert held is not None
        assert held.holder == "owner-terminal"
        assert finished.get_extra()["attested_state"] == initial

    def test_survivor_keeps_claim_and_prior_attestation(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        with patch(
            "teatree.core.worktree.checkout_fence.read_process_table",
            return_value=ProcessTable(frozenset(), "/proc"),
        ):
            verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        with (
            patch("teatree.core.worktree.occupancy.page_surviving_checkout") as page,
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

        page.assert_called_once()

        finished = Worktree.objects.get(pk=self.worktree.pk)
        held = occupancy_holder(finished)
        assert held is not None
        assert held.holder == task_holder_id(self.task)
        assert finished.get_extra()["attested_state"] == initial

    def test_cancelled_run_does_not_attest_partial_tree(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        clear = ProcessTable(frozenset(), "/proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        with (
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=clear),
            occupy_ticket_checkout(self.ticket, holder=task_holder_id(self.task), holder_session="s1"),
            drive_claim(self.task),
        ):
            (self.checkout / "partial.txt").write_text("half done")
            Task.objects.get(pk=self.task.pk).fail(reason="cancelled: owner stop", by_holder=False)
        finished = Worktree.objects.get(pk=self.worktree.pk)
        assert occupancy_holder(finished) is None
        assert finished.get_extra()["attested_state"] == initial

    def test_process_wait_precedes_terminal_write(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        clear = ProcessTable(frozenset(), "/proc")

        def settled(*_args: object, **_kwargs: object) -> tuple[()]:
            assert Task.objects.get(pk=self.task.pk).status == Task.Status.CLAIMED
            return ()

        with (
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=clear),
            patch("teatree.core.worktree.occupancy.wait_for_owned_checkout_processes", side_effect=settled) as wait,
            patch("teatree.core.worktree.occupancy.page_surviving_checkout") as page,
            occupy_ticket_checkout(self.ticket, holder=task_holder_id(self.task), holder_session="s1"),
            drive_claim(self.task),
        ):
            self.task.complete()
        wait.assert_called_once()
        page.assert_not_called()
        assert occupancy_holder(Worktree.objects.get(pk=self.worktree.pk)) is None

    def test_deploy_checkpoint_attests_own_edit_for_same_task_resume(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        clear = ProcessTable(frozenset(), "/proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]

        with (
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=clear),
            patch("teatree.core.worktree.occupancy.page_surviving_checkout") as page,
            occupy_ticket_checkout(
                self.ticket, holder=task_holder_id(self.task), holder_session=self.task.claimed_by_session
            ),
        ):
            (self.checkout / "new.txt").write_text("checkpointed run output")
            TaskAttempt.objects.create(
                task=self.task,
                ended_at=timezone.now(),
                exit_code=1,
                error="limit_parked: deploy checkpoint: worker quiescing",
                agent_session_id="0f1e2d3c-4b5a-4968-8776-655443322110",
            )
            self.task.park(not_before=timezone.now())

        page.assert_not_called()
        finished = Worktree.objects.get(pk=self.worktree.pk)
        assert occupancy_holder(finished) is None
        assert finished.get_extra()["attested_state"] != initial
        self.task.claim(claimed_by="worker-2", claimed_by_session="s2")
        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear),
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=clear),
            patch.object(runner, "_run_agent", return_value=Mock(spec=TaskAttempt)) as drive,
            patch("teatree.core.worktree.occupancy.page_surviving_checkout") as resume_page,
        ):
            runner.run_agent(self.task, phase="coding", overlay_skill_metadata={})
        drive.assert_called_once()
        resume_page.assert_not_called()

    def test_other_park_does_not_attest_checkout_edit(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        clear = ProcessTable(frozenset(), "/proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]

        with (
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=clear),
            occupy_ticket_checkout(
                self.ticket, holder=task_holder_id(self.task), holder_session=self.task.claimed_by_session
            ),
        ):
            (self.checkout / "new.txt").write_text("unverified edit")
            TaskAttempt.objects.create(
                task=self.task, ended_at=timezone.now(), exit_code=1, error="limit_parked: provider budget"
            )
            self.task.park(not_before=timezone.now())

        assert Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"] == initial
        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear),
            pytest.raises(CheckoutFenceError, match="attested"),
        ):
            verify_task_checkout(self.task, str(self.checkout))

    def test_old_checkpoint_attempt_cannot_attest_a_new_run(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        clear = ProcessTable(frozenset(), "/proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        TaskAttempt.objects.create(
            task=self.task,
            ended_at=timezone.now() - timedelta(seconds=1),
            exit_code=1,
            error="limit_parked: deploy checkpoint: earlier run",
        )

        with (
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=clear),
            occupy_ticket_checkout(
                self.ticket, holder=task_holder_id(self.task), holder_session=self.task.claimed_by_session
            ),
        ):
            (self.checkout / "new.txt").write_text("later run output")
            self.task.park(not_before=timezone.now())

        assert Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"] == initial
        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear),
            pytest.raises(CheckoutFenceError, match="attested"),
        ):
            verify_task_checkout(self.task, str(self.checkout))
