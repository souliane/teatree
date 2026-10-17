from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import pytest
from django.test import TestCase
from django.utils import timezone

from teatree.agents import runner
from teatree.agents.attempt_recorder import AttemptUsage
from teatree.agents.runner_interruption import _record_checkpoint
from teatree.agents.runner_route_recording import park_after_route_failure
from teatree.agents.usage_window import record_park
from teatree.core.cleanup.process_table import ProcessTable
from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Task, TaskAttempt, Worktree
from teatree.core.models.task_claim import drive_claim
from teatree.core.models.task_repair import check_requeue_allowed, phase_attempts
from teatree.core.owner_question_message import render_text
from teatree.core.worktree.checkout_fence import (
    CheckoutFenceError,
    claim_task_checkout_or_park,
    park_task_checkout,
    verify_task_checkout,
    writes_checkout,
)
from teatree.core.worktree.checkout_state import checkout_state
from teatree.core.worktree.occupancy import (
    WorktreeOccupiedError,
    acquire,
    occupancy_holder,
    occupy_ticket_checkout,
    refuse_if_ticket_checkout_occupied,
    release,
    task_holder_id,
)
from teatree.loop.question_binding import answer_from_click
from teatree.utils.git import run_strict
from tests.factories import TaskFactory, TicketFactory, WorktreeFactory
from tests.teatree_core._self_review_helpers import completed_self_review
from tests.teatree_core.models._shared import _init_repo_with_branch

_CLEAR = ProcessTable(frozenset(), "/proc")


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

    def _park_on_a_foreign_edit(self) -> None:
        verify_task_checkout(self.task, str(self.checkout))
        (self.checkout / "new.txt").write_text("foreign edit")
        attempt = claim_task_checkout_or_park(self.task)
        assert attempt is not None
        assert "differs from its attested state" in attempt.error

    def test_a_foreign_edit_parks_with_one_owner_card_instead_of_a_notice(self) -> None:
        self.ticket.issue_url = "https://github.com/example/repo/issues/17"
        self.ticket.save(update_fields=["issue_url"])
        with (
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=_CLEAR),
            patch("teatree.core.worktree.checkout_fence.notify_user") as notice,
        ):
            self._park_on_a_foreign_edit()
            assert claim_task_checkout_or_park(self.task) is not None

        notice.assert_not_called()
        row = self.task.deferred_questions.get()
        assert row.is_pending
        assert row.question.startswith("Should I accept the checkout of <https://github.com/example/repo/issues/17|")
        assert row.evidence["checked"] == [
            "The checkout differs from its last recorded state.",
            "Nothing is running in the checkout.",
            "It holds 0 commits not on the target branch.",
            "It has 1 uncommitted file.",
        ]
        assert "Nothing is running in the checkout" in render_text(row)
        assert row.evidence["action"]["kind"] == "attest"

    def test_an_answered_card_falls_back_to_the_notice_for_the_same_state(self) -> None:
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=_CLEAR):
            self._park_on_a_foreign_edit()
            row = self.task.deferred_questions.get()
            assert row.mark_mirrored(channel="D0DEMOOWNER", slack_ts="1800000000.000100")
            assert answer_from_click(
                {
                    "user": {"id": "U0DEMOOWNER"},
                    "channel": {"id": row.slack_channel},
                    "message": {"ts": row.slack_ts},
                    "actions": [{"action_id": "owner-question-option-2", "value": "2", "text": {"text": "Decline"}}],
                },
                backend=Mock(user_id="U0DEMOOWNER"),
            )
            with patch("teatree.core.worktree.checkout_fence.notify_user") as notice:
                assert claim_task_checkout_or_park(self.task) is not None

        notice.assert_called_once()
        assert self.task.deferred_questions.count() == 1

    def test_next_writer_reattests_after_an_interrupted_holder(self) -> None:
        Worktree.objects.filter(pk=self.worktree.pk).update(branch="feature")
        self.worktree.refresh_from_db()
        clear = ProcessTable(frozenset(), "/proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(self.task, str(self.checkout))
            previous = TaskFactory(ticket=self.ticket, phase="coding")
            acquire(self.worktree, holder=task_holder_id(previous))
            (self.checkout / "change.txt").write_text("work from interrupted run")
            run_strict(repo=str(self.checkout), args=["config", "user.email", "test@example.com"])
            run_strict(repo=str(self.checkout), args=["config", "user.name", "Test"])
            run_strict(repo=str(self.checkout), args=["add", "change.txt"])
            run_strict(repo=str(self.checkout), args=["commit", "-m", "interrupted work"])
            release(self.worktree, holder=task_holder_id(previous))
            next_task = TaskFactory(ticket=self.ticket, phase="testing")
            acquire(self.worktree, holder=task_holder_id(next_task))
            verify_task_checkout(next_task, str(self.checkout))
        self.worktree.refresh_from_db()
        assert self.worktree.get_extra()["attested_state"] == checkout_state(self.checkout).digest
        assert self.worktree.get_extra()["auto_attestation"]["tenures"][0]["holder"] == task_holder_id(previous)

    def test_checkout_parks_do_not_spend_repair_budget(self) -> None:
        for _ in range(5):
            attempt = park_task_checkout(self.task, reason="foreign checkout state")
            assert attempt.error.startswith("checkout_parked:")
        assert phase_attempts(self.task) == []
        check_requeue_allowed(self.task)

    def test_a_checkout_park_is_not_a_usage_window_and_keeps_route_failures(self) -> None:
        failed = TaskAttempt.objects.create(
            task=self.task,
            ended_at=timezone.now(),
            exit_code=1,
            error="result_error: the run ended without a clean result",
            route_source_skill="t3:code",
            route_candidate_index=0,
        )
        attempt = park_task_checkout(self.task, reason="foreign checkout state")
        assert attempt.failure_kind != FailureKind.USAGE_LIMIT_PARKED
        assert self.task.attempts.failed_route_candidates("t3:code") == {0: failed.pk}

    def test_a_park_retries_at_the_holders_lease_expiry_or_thirty_minutes(self) -> None:
        before = timezone.now()
        park_task_checkout(self.task, reason="foreign checkout state")
        self.task.refresh_from_db()
        assert before + timedelta(minutes=29) < self.task.not_before <= timezone.now() + timedelta(minutes=30)

        held = acquire(self.worktree, holder="owner-terminal", holder_session="owner", lease_seconds=120)
        park_task_checkout(self.task, reason="held by the owner")
        self.task.refresh_from_db()
        assert self.task.not_before == held.expires_at

    def test_one_page_per_episode_until_a_run_succeeds(self) -> None:
        with patch("teatree.core.worktree.checkout_fence.notify_user") as page:
            park_task_checkout(self.task, reason="foreign checkout state")
            park_task_checkout(self.task, reason="foreign checkout state, again")
            TaskAttempt.objects.create(task=self.task, ended_at=timezone.now(), exit_code=0)
            park_task_checkout(self.task, reason="foreign checkout state")
        keys = [call.kwargs["idempotency_key"] for call in page.call_args_list]
        assert keys[0] == keys[1] != keys[2]

    def test_writing_phases_are_the_ones_with_write_tools(self) -> None:
        assert writes_checkout(TaskFactory(phase="retro"))
        assert writes_checkout(TaskFactory(phase="architectural_review"))
        assert not writes_checkout(TaskFactory(phase="reviewing"))

    def test_debugging_at_a_reviewed_head_on_an_attested_checkout_runs(self) -> None:
        debugging = TaskFactory(ticket=self.ticket, phase="debugging")
        verify_task_checkout(debugging, str(self.checkout))
        head = run_strict(repo=str(self.checkout), args=["rev-parse", "HEAD"])
        completed_self_review(self.ticket, "merge_safe", reviewed_sha=head)
        debugging.claim(claimed_by="worker", claimed_by_session="s1")
        with patch.object(runner, "_run_agent", return_value=Mock(spec=TaskAttempt)) as drive:
            runner.run_agent(debugging, phase="debugging", overlay_skill_metadata={})
        drive.assert_called_once()

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
        with pytest.raises(WorktreeOccupiedError):
            refuse_if_ticket_checkout_occupied(self.ticket)
        with (
            pytest.raises(WorktreeOccupiedError),
            occupy_ticket_checkout(self.ticket, holder="task:999", holder_session="s2"),
        ):
            pass

    def test_deferred_exit_is_attested_by_the_next_claim_after_lease_lapse(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        clear = ProcessTable(frozenset(), "/host-proc")
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        busy = ProcessTable(frozenset({self.checkout}), "/host-proc")
        with (
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=busy),
            patch("teatree.core.worktree.occupancy.page_surviving_checkout"),
            occupy_ticket_checkout(self.ticket, holder=task_holder_id(self.task), holder_session="s1"),
            drive_claim(self.task),
        ):
            (self.checkout / "new.txt").write_text("run output")
            self.task.complete()
        row = Worktree.objects.get(pk=self.worktree.pk)
        assert row.get_extra()["attested_state"] == initial
        assert row.get_extra()["deferred_exit"]["digest"] == checkout_state(self.checkout).digest
        Worktree.objects.filter(pk=row.pk).update(occupancy_expires_at=timezone.now() - timedelta(seconds=1))
        next_task = TaskFactory(ticket=self.ticket, phase="testing")
        next_task.claim(claimed_by="worker", claimed_by_session="s2")
        container_table = ProcessTable(frozenset(), "/host-proc", (), ("pid 7 unreadable by this uid",))
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=container_table):
            assert claim_task_checkout_or_park(next_task) is None
        row.refresh_from_db()
        assert row.get_extra()["attested_state"] == checkout_state(self.checkout).digest
        audit = row.get_extra()["auto_attestation"]
        assert "deferred_exit" in audit
        deferred = audit["deferred_exit"]
        assert isinstance(deferred, dict)
        assert deferred["session"] == "s1"

    def test_a_finished_holder_whose_checkout_is_busy_is_not_self_healed(self) -> None:
        verify_task_checkout(self.task, str(self.checkout))
        Task.objects.filter(pk=self.task.pk).update(status=Task.Status.COMPLETED)
        acquire(self.worktree, holder=task_holder_id(self.task), holder_session="")
        busy = ProcessTable(frozenset({self.checkout}), "/proc")
        with (
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=busy),
            pytest.raises(WorktreeOccupiedError),
        ):
            refuse_if_ticket_checkout_occupied(self.ticket)
        refuse_if_ticket_checkout_occupied(self.ticket)

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
            _record_checkpoint(self.task, checkpoint="deploy checkpoint: worker quiescing", usage=AttemptUsage())

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

    def test_usage_window_and_route_failure_parks_attest_the_runs_own_edit(self) -> None:
        clear = ProcessTable(frozenset(), "/proc")
        verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        parks = (
            lambda: record_park(
                self.task,
                reason="limit_parked: subscription_session: window",
                not_before=timezone.now(),
                usage=AttemptUsage(),
            ),
            lambda: park_after_route_failure(
                self.task,
                Mock(),
                Mock(hold=None),
                phase="coding",
            ),
        )
        for number, park in enumerate(parks):
            self.task.claim(claimed_by="worker", claimed_by_session=f"s{number}")
            with occupy_ticket_checkout(self.ticket, holder=task_holder_id(self.task), holder_session=f"s{number}"):
                (self.checkout / f"edit{number}.txt").write_text("the run's own edit")
                park()
            finished = Worktree.objects.get(pk=self.worktree.pk)
            assert occupancy_holder(finished) is None
            assert finished.get_extra()["attested_state"] == checkout_state(self.checkout).digest != initial
        with patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=clear):
            verify_task_checkout(self.task, str(self.checkout))

    def test_a_pre_dispatch_park_releases_without_attesting(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        with occupy_ticket_checkout(self.ticket, holder=task_holder_id(self.task), holder_session="s1"):
            (self.checkout / "foreign.txt").write_text("not this run's work")
            record_park(self.task, reason="limit_parked: admission: window", not_before=timezone.now())
        finished = Worktree.objects.get(pk=self.worktree.pk)
        assert occupancy_holder(finished) is None
        assert finished.get_extra()["attested_state"] == initial

    def test_a_refused_run_leaves_its_attestation_unchanged(self) -> None:
        verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        (self.checkout / "foreign.txt").write_text("foreign")
        self.task.claim(claimed_by="worker", claimed_by_session="s9")
        with patch.object(runner, "_run_agent", return_value=Mock(spec=TaskAttempt)) as drive:
            runner.run_agent(self.task, phase="coding", overlay_skill_metadata={})
        drive.assert_not_called()
        finished = Worktree.objects.get(pk=self.worktree.pk)
        assert Task.objects.get(pk=self.task.pk).status == Task.Status.PENDING
        assert finished.get_extra()["attested_state"] == initial
        assert occupancy_holder(finished) is None

    def test_an_unusable_table_releases_the_exit_unattested_and_still_refuses(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        unavailable = ProcessTable(frozenset(), "", ("containerised with no host process table",))
        with (
            patch("teatree.core.worktree.occupancy.read_process_table", return_value=unavailable),
            patch("teatree.core.worktree.checkout_fence.read_process_table", return_value=unavailable),
            patch("teatree.core.worktree.occupancy.page_surviving_checkout") as page,
        ):
            with (
                occupy_ticket_checkout(self.ticket, holder=task_holder_id(self.task), holder_session="s1"),
                drive_claim(self.task),
            ):
                (self.checkout / "new.txt").write_text("run output")
                self.task.complete()
            finished = Worktree.objects.get(pk=self.worktree.pk)
            assert occupancy_holder(finished) is None
            assert finished.get_extra()["attested_state"] == initial
            page.assert_not_called()
            with pytest.raises(CheckoutFenceError, match="process table unavailable"):
                verify_task_checkout(TaskFactory(ticket=self.ticket, phase="testing"), str(self.checkout))

    def test_a_failing_run_waits_for_its_own_processes_before_its_terminal_write(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")

        def settled(*_args: object, **_kwargs: object) -> tuple[()]:
            assert Task.objects.get(pk=self.task.pk).status == Task.Status.CLAIMED
            return ()

        with (
            patch("teatree.core.worktree.occupancy.wait_for_owned_checkout_processes", side_effect=settled) as wait,
            occupy_ticket_checkout(self.ticket, holder=task_holder_id(self.task), holder_session="s1"),
            drive_claim(self.task),
        ):
            self.task.fail(reason="result_error: the run broke", by_holder=True)
        wait.assert_called_once()
        assert occupancy_holder(Worktree.objects.get(pk=self.worktree.pk)) is None

    def test_a_reaped_run_waits_for_its_processes_and_never_attests(self) -> None:
        self.task.claim(claimed_by="worker", claimed_by_session="s1")
        verify_task_checkout(self.task, str(self.checkout))
        initial = Worktree.objects.get(pk=self.worktree.pk).get_extra()["attested_state"]
        with (
            patch("teatree.core.worktree.occupancy.wait_for_owned_checkout_processes", return_value=()) as wait,
            occupy_ticket_checkout(self.ticket, holder=task_holder_id(self.task), holder_session="s1"),
            drive_claim(self.task),
        ):
            (self.checkout / "partial.txt").write_text("half done")
            Task.objects.get(pk=self.task.pk).fail(reason="lease_expired: reaped", by_holder=False)
        wait.assert_called_once()
        finished = Worktree.objects.get(pk=self.worktree.pk)
        assert occupancy_holder(finished) is None
        assert finished.get_extra()["attested_state"] == initial

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
