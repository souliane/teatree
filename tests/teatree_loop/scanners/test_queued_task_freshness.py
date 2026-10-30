from datetime import timedelta
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from teatree.core.models.errors import NoCurrentPlanError
from teatree.core.models.task import Task
from teatree.core.models.ticket import Ticket
from teatree.core.overlay import OverlayBase
from teatree.loop.scanners.queued_task_freshness import QueuedTaskFreshnessScanner, _remote_branch_state
from tests.factories import SessionFactory, TaskFactory, TicketFactory, WorktreeFactory


class _Overlay(OverlayBase):
    def get_repos(self) -> list[str]:
        return []

    def get_provision_steps(self, worktree: Any) -> list[Any]:
        return []

    def is_issue_done(self, issue_data: dict[str, object]) -> bool:
        return issue_data.get("state") == "closed"


class _Host:
    def __init__(self, state: str = "open") -> None:
        self.state = state

    def get_issue(self, issue_url: str) -> dict[str, str]:
        return {"state": self.state}


class QueuedTaskFreshnessTests(TestCase):
    def setUp(self) -> None:
        self.ticket = TicketFactory(
            overlay="t3-test", issue_url="https://example.com/org/repo/issues/12", extra={"target_branch": "stacked"}
        )
        self.task = TaskFactory(ticket=self.ticket, session=SessionFactory(ticket=self.ticket), phase="coding")
        Task.objects.filter(pk=self.task.pk).update(created_at=timezone.now() - timedelta(hours=25))
        self.task.refresh_from_db()
        WorktreeFactory(ticket=self.ticket, branch="feature", extra={"worktree_path": str(Path.cwd())})
        self.scanner = QueuedTaskFreshnessScanner(overlay=_Overlay(), overlay_name="t3-test")

    def test_missing_target_notes_once_per_window_and_rechecks_after_window(self) -> None:
        with (
            patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()),
            patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", return_value="missing") as probe,
        ):
            first = self.scanner.scan()
            second = self.scanner.scan()
            self.ticket.refresh_from_db()
            stamps = self.ticket.extra["last_validated_at"]
            self.task.refresh_from_db()
            assert len(first) == 1
            assert second == []
            assert probe.call_count == 1
            assert str(self.task.pk) in stamps
            assert "premise changed" in self.task.execution_reason
            assert "stacked" in self.ticket.context
            self.ticket.merge_extra(
                merge_into_dicts={"last_validated_at": {str(self.task.pk): "2020-01-01T00:00:00+00:00"}}
            )
            assert len(self.scanner.scan()) == 1
            assert probe.call_count == 2

    def test_young_task_is_untouched(self) -> None:
        Task.objects.filter(pk=self.task.pk).update(created_at=timezone.now() - timedelta(hours=2))
        assert self.scanner.scan() == []
        self.ticket.refresh_from_db()
        assert "last_validated_at" not in self.ticket.extra

    def test_landed_branch_cancels_pending_task_with_ticket_reason(self) -> None:
        with (
            patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()),
            patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", return_value="present"),
            patch("teatree.loop.scanners.queued_task_freshness.branch_content_landed_on_base", return_value=True),
        ):
            signals = self.scanner.scan()
        self.task.refresh_from_db()
        self.ticket.refresh_from_db()
        assert self.task.status == Task.Status.FAILED
        assert "superseded" in self.task.failure_reason
        assert "already landed" in self.ticket.context
        assert len(signals) == 1

    def test_open_issue_and_current_branch_leave_task_pending(self) -> None:
        with (
            patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()),
            patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", return_value="present"),
            patch("teatree.loop.scanners.queued_task_freshness.branch_content_landed_on_base", return_value=False),
            patch("teatree.loop.scanners.queued_task_freshness.check_plan_current", return_value=True),
        ):
            assert self.scanner.scan() == []
        self.task.refresh_from_db()
        self.ticket.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
        assert str(self.task.pk) in self.ticket.extra["last_validated_at"]
        assert "premise changed" not in self.task.execution_reason

    def test_unreadable_issue_keeps_task_pending_and_names_uncertainty(self) -> None:
        with patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=None):
            signals = self.scanner.scan()
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
        assert "premise unverifiable: issue host unavailable" in self.task.execution_reason
        assert len(signals) == 1

    def test_synthetic_anchor_is_not_rechecked_as_a_forge_issue(self) -> None:
        self.ticket.issue_url = "backlog-sweep://t3-test"
        self.ticket.save(update_fields=["issue_url"])
        with patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", side_effect=AssertionError):
            assert self.scanner.scan() == []
        self.ticket.refresh_from_db()
        assert "last_validated_at" not in self.ticket.extra

    def test_local_anchor_checks_checkout_without_reading_a_forge_issue(self) -> None:
        self.ticket.issue_url = "auto:feature"
        self.ticket.save(update_fields=["issue_url"])
        with (
            patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", side_effect=AssertionError),
            patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", return_value="missing"),
        ):
            signals = self.scanner.scan()
        assert len(signals) == 1
        assert "target branch" in signals[0].summary

    def test_closed_issue_supersedes_pending_task(self) -> None:
        with patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host("closed")):
            signals = self.scanner.scan()
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.FAILED
        assert "issue already closed" in self.task.failure_reason
        assert len(signals) == 1

    def test_stale_plan_supersedes_coding_and_queues_a_new_plan(self) -> None:
        Ticket.objects.filter(pk=self.ticket.pk).update(state=Ticket.State.PLAN_RECORDED)
        self.ticket.refresh_from_db()
        with (
            patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()),
            patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", return_value="present"),
            patch("teatree.loop.scanners.queued_task_freshness.branch_content_landed_on_base", return_value=False),
            patch(
                "teatree.loop.scanners.queued_task_freshness.check_plan_current",
                side_effect=NoCurrentPlanError("seams moved"),
            ),
        ):
            signals = self.scanner.scan()
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.FAILED
        assert "plan no longer current" in self.task.failure_reason
        assert Task.objects.filter(ticket=self.ticket, phase="planning", status=Task.Status.PENDING).exists()
        assert len(signals) == 1

    def test_stale_plan_does_not_queue_a_plan_after_the_coding_task_is_claimed(self) -> None:
        Ticket.objects.filter(pk=self.ticket.pk).update(state=Ticket.State.PLAN_RECORDED)
        self.ticket.refresh_from_db()

        def claim_during_probe(repo: str, branch: str) -> str:
            Task.objects.filter(pk=self.task.pk).update(status=Task.Status.CLAIMED)
            return "present"

        with (
            patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()),
            patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", side_effect=claim_during_probe),
            patch("teatree.loop.scanners.queued_task_freshness.branch_content_landed_on_base", return_value=False),
            patch(
                "teatree.loop.scanners.queued_task_freshness.check_plan_current",
                side_effect=NoCurrentPlanError("stale"),
            ),
        ):
            self.scanner.scan()
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.CLAIMED
        assert not Task.objects.filter(ticket=self.ticket, phase="planning", status=Task.Status.PENDING).exists()

    def test_closed_issue_is_not_a_cancellation_of_shipping(self) -> None:
        Task.objects.filter(pk=self.task.pk).update(phase="shipping")
        self.task.refresh_from_db()
        with patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host("closed")):
            signals = self.scanner.scan()
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
        assert "issue already closed" in self.task.execution_reason
        assert len(signals) == 1

    def test_stale_plan_does_not_cancel_a_ticket_outside_the_coding_gate(self) -> None:
        with (
            patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()),
            patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", return_value="present"),
            patch("teatree.loop.scanners.queued_task_freshness.branch_content_landed_on_base", return_value=False),
            patch(
                "teatree.loop.scanners.queued_task_freshness.check_plan_current",
                side_effect=NoCurrentPlanError("seams moved"),
            ) as plan,
        ):
            assert self.scanner.scan() == []
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
        plan.assert_not_called()

    def test_stamped_old_tasks_do_not_starve_a_new_candidate(self) -> None:
        old = [TaskFactory(ticket=self.ticket, session=self.task.session, phase="coding") for _ in range(51)]
        Task.objects.filter(pk__in=[task.pk for task in old]).update(created_at=timezone.now() - timedelta(hours=26))
        self.ticket.merge_extra(
            set_keys={"last_validated_at": {str(task.pk): timezone.now().isoformat() for task in old}}
        )
        with (
            patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()),
            patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", return_value="missing"),
        ):
            signals = self.scanner.scan()
        assert [signal.payload["task_id"] for signal in signals] == [self.task.pk]

    def test_recent_stamp_skips_the_write_lock(self) -> None:
        self.ticket.merge_extra(set_keys={"last_validated_at": {str(self.task.pk): timezone.now().isoformat()}})
        with patch("teatree.loop.scanners.queued_task_freshness.transaction.atomic", side_effect=AssertionError):
            assert self.scanner.scan() == []

    def test_recheck_stops_at_its_tick_budget(self) -> None:
        second = TaskFactory(ticket=self.ticket, session=self.task.session, phase="coding")
        Task.objects.filter(pk=second.pk).update(created_at=timezone.now() - timedelta(hours=24, minutes=30))
        times = iter((0.0, 0.0, 11.0))
        scanner = QueuedTaskFreshnessScanner(overlay=_Overlay(), overlay_name="t3-test", monotonic=lambda: next(times))
        with patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=None):
            signals = scanner.scan()
        assert [signal.payload["task_id"] for signal in signals] == [self.task.pk]
        self.ticket.refresh_from_db()
        assert str(second.pk) not in self.ticket.extra["last_validated_at"]

    def test_remote_target_probe_distinguishes_missing_from_unreadable(self) -> None:
        for code, expected in ((0, "present"), (2, "missing"), (128, "unknown")):
            with (
                self.subTest(code=code),
                patch(
                    "teatree.loop.scanners.queued_task_freshness.run_bounded_group",
                    return_value=CompletedProcess([], code),
                ),
            ):
                assert _remote_branch_state(str(Path.cwd()), "origin/stacked") == expected
        with patch("teatree.loop.scanners.queued_task_freshness.run_bounded_group", side_effect=OSError):
            assert _remote_branch_state(str(Path.cwd()), "origin/stacked") == "unknown"

    def test_remote_target_probe_disables_credential_prompts(self) -> None:
        with patch(
            "teatree.loop.scanners.queued_task_freshness.run_bounded_group",
            return_value=CompletedProcess([], 0),
        ) as run:
            assert _remote_branch_state(str(Path.cwd()), "origin/stacked") == "present"
        assert run.call_args.kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"

    def test_bad_validation_stamp_is_replaced_by_a_current_one(self) -> None:
        for bad in ("not-a-date", "2020-01-01T00:00:00"):
            with self.subTest(bad=bad):
                self.ticket.merge_extra(set_keys={"last_validated_at": {str(self.task.pk): bad}})
                with (
                    patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()),
                    patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", return_value="present"),
                    patch(
                        "teatree.loop.scanners.queued_task_freshness.branch_content_landed_on_base", return_value=False
                    ),
                ):
                    assert self.scanner.scan() == []
                self.ticket.refresh_from_db()
                assert self.ticket.extra["last_validated_at"][str(self.task.pk)] != bad

    def test_target_probe_uncertainty_is_in_the_agent_reason(self) -> None:
        with (
            patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()),
            patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", return_value="unknown"),
        ):
            assert len(self.scanner.scan()) == 1
        self.task.refresh_from_db()
        assert "premise unverifiable: target branch could not be read" in self.task.execution_reason

    def test_no_checkout_does_not_claim_the_branch_landed(self) -> None:
        self.ticket.worktrees.update(extra={})
        with patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()):
            assert self.scanner.scan() == []
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING

    def test_host_error_is_reported_without_cancelling_the_task(self) -> None:
        class FailingHost:
            def get_issue(self, issue_url: str) -> dict[str, str]:
                raise OSError

        with patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=FailingHost()):
            assert len(self.scanner.scan()) == 1
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
        assert "premise unverifiable: issue could not be read" in self.task.execution_reason

    def test_malformed_issue_is_reported_without_cancelling_the_task(self) -> None:
        class MalformedHost:
            def get_issue(self, issue_url: str) -> dict[str, str]:
                return {"error": "bad response"}

        with patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=MalformedHost()):
            assert len(self.scanner.scan()) == 1
        self.task.refresh_from_db()
        assert self.task.status == Task.Status.PENDING
        assert "premise unverifiable: issue could not be read" in self.task.execution_reason

    def test_unexpected_probe_failure_is_stamped_as_unverifiable(self) -> None:
        with patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", side_effect=KeyError):
            assert len(self.scanner.scan()) == 1
        self.task.refresh_from_db()
        assert "premise unverifiable: recheck failed" in self.task.execution_reason

    def test_claim_during_probe_is_never_cancelled_by_a_stale_landed_verdict(self) -> None:
        def claim_during_probe(repo: str, branch: str) -> str:
            Task.objects.filter(pk=self.task.pk).update(status=Task.Status.CLAIMED)
            return "present"

        with (
            patch("teatree.loop.scanners.queued_task_freshness.get_code_host_for_url", return_value=_Host()),
            patch("teatree.loop.scanners.queued_task_freshness._remote_branch_state", side_effect=claim_during_probe),
            patch("teatree.loop.scanners.queued_task_freshness.branch_content_landed_on_base", return_value=True),
        ):
            assert len(self.scanner.scan()) == 1
        self.task.refresh_from_db()
        self.ticket.refresh_from_db()
        assert self.task.status == Task.Status.CLAIMED
        assert "premise changed after claim" in self.ticket.context
