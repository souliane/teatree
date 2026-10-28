"""The worker owns every write following a read-only sweep proposal."""

from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase

from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope
from teatree.core.models import DeferredQuestion, Session, Task, Ticket, TicketSweepRun


class TestSweepProposalRecording(TestCase):
    def setUp(self) -> None:
        self.ticket = Ticket.objects.create(overlay="example", role=Ticket.Role.AUTHOR)
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket),
            phase="backlog_sweep",
            execution_reason="ASK-GATE: return proposals",
        )
        self.task.claim(claimed_by="loop-slot")

    def _record(
        self,
        proposals: object,
        *,
        harness: str = "codex_app_server",
        expected_examined: list[str] | None = None,
        index_urls: list[str] | None = None,
    ) -> object:
        provider = SimpleNamespace(get_code_host_for_url=lambda _overlay, _url: object())
        examined = proposals.get("examined", []) if isinstance(proposals, dict) else []
        expected = expected_examined if expected_examined is not None else examined
        with (
            patch("teatree.agents.sweep_proposals_recorder.get_overlay_for_ticket", return_value="overlay"),
            patch(
                "teatree.agents.sweep_proposals_recorder.owned_repo_slugs", return_value=("example-org/example-repo",)
            ),
            patch("teatree.agents.sweep_proposals_recorder.get_backend_provider", return_value=provider),
            patch(
                "teatree.agents.sweep_proposals_recorder.sweep_window_evidence",
                return_value=(index_urls if index_urls is not None else expected, expected, 0),
            ),
        ):
            return record_result_envelope(
                self.task,
                {"summary": "classified", "sweep_proposals": proposals},
                phase="backlog_sweep",
                usage=AttemptUsage(selected_harness=harness),
            )

    def test_empty_executor_proposals_finish_a_measured_zero_run(self) -> None:
        attempt = self._record({"examined": [], "folds": [], "gaps": []})

        run = TicketSweepRun.objects.get()
        assert attempt.error == ""
        assert run.source == "loop"
        assert run.finished_at is not None
        assert run.examined_count == 0
        assert run.changed_count == 0

    def test_folds_are_stored_and_ask_once_without_forge_mutation(self) -> None:
        member = "https://github.com/example-org/example-repo/issues/1"
        host = "https://github.com/example-org/example-repo/issues/2"
        proposals = {
            "examined": [member, host],
            "folds": [{"member_url": member, "host_url": host, "citation": "same module"}],
            "gaps": [],
        }
        with patch(
            "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
        ):
            attempt = self._record(proposals)

        self.ticket.refresh_from_db()
        assert attempt.error == ""
        assert len(self.ticket.extra["sweep_fold_proposals"]) == 1
        assert DeferredQuestion.objects.filter(parked_task=self.task).count() == 1
        assert TicketSweepRun.objects.get().examined_count == 2

    def test_fold_host_may_come_from_the_index_outside_the_body_window(self) -> None:
        member = "https://github.com/example-org/example-repo/issues/1"
        host = "https://github.com/example-org/example-repo/issues/2"
        proposals = {
            "examined": [member],
            "folds": [{"member_url": member, "host_url": host, "citation": "same module"}],
            "gaps": [],
        }
        with patch(
            "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
        ):
            attempt = self._record(proposals, index_urls=[member, host])

        assert attempt.error == ""
        assert TicketSweepRun.objects.get().examined_count == 1

    def test_fold_host_outside_the_index_refuses_before_any_write(self) -> None:
        member = "https://github.com/example-org/example-repo/issues/1"
        host = "https://github.com/example-org/example-repo/issues/2"
        proposals = {
            "examined": [member],
            "folds": [{"member_url": member, "host_url": host, "citation": "same module"}],
            "gaps": [],
        }
        attempt = self._record(proposals)

        assert "indexed issue" in attempt.error
        assert TicketSweepRun.objects.count() == 0

    def test_invalid_member_refuses_before_any_write(self) -> None:
        proposals = {
            "examined": [],
            "folds": [{"member_url": "external", "host_url": "host", "citation": "x"}],
            "gaps": [],
        }

        attempt = self._record(proposals)

        assert attempt.error
        assert TicketSweepRun.objects.count() == 0
        assert DeferredQuestion.objects.count() == 0
        self.ticket.refresh_from_db()
        assert "sweep_fold_proposals" not in self.ticket.extra

    def test_non_self_authored_fold_refuses_before_any_write(self) -> None:
        member = "https://github.com/example-org/example-repo/issues/1"
        host = "https://github.com/example-org/example-repo/issues/2"
        proposals = {
            "examined": [member, host],
            "folds": [{"member_url": member, "host_url": host, "citation": "same module"}],
            "gaps": [],
        }
        with patch(
            "teatree.agents.sweep_proposals_recorder.require_self_authored_issue",
            side_effect=ValueError("not self-authored"),
        ):
            attempt = self._record(proposals)

        assert "not self-authored" in attempt.error
        assert TicketSweepRun.objects.count() == 0
        assert DeferredQuestion.objects.count() == 0
        self.ticket.refresh_from_db()
        assert "sweep_fold_proposals" not in self.ticket.extra

    def test_claude_still_requires_ticket_sweep(self) -> None:
        attempt = self._record({"examined": [], "folds": [], "gaps": []}, harness="claude_sdk")

        assert "ticket_sweep" in attempt.error
        assert TicketSweepRun.objects.count() == 0

    def test_empty_examined_list_cannot_advance_a_nonempty_window(self) -> None:
        expected = ["https://github.com/example-org/example-repo/issues/1"]
        attempt = self._record({"examined": [], "folds": [], "gaps": []}, expected_examined=expected)

        assert "examined" in attempt.error
        assert TicketSweepRun.objects.count() == 0

    def test_gap_attachment_uses_the_workers_command_and_measured_run(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_ticket_id": host.pk, "gap_keys": ["gap-a"]}]}
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue",
                return_value={"state": "open"},
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
        ):
            attempt = self._record(proposals)

        assert attempt.error == ""
        run = TicketSweepRun.objects.get()
        assert run.finished_at is not None
        assert attach.call_args.args[:3] == ("ticket", "attach-gaps", str(host.pk))
        assert attach.call_args.args[-1] == run.run_id

    def test_invalid_fold_after_valid_gap_refuses_before_the_command(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {
            "examined": [],
            "folds": [{"member_url": "external", "host_url": "host", "citation": "same module"}],
            "gaps": [{"host_ticket_id": host.pk, "gap_keys": ["gap-a"]}],
        }
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
        ):
            attempt = self._record(proposals)

        assert attempt.error
        attach.assert_not_called()
        assert TicketSweepRun.objects.count() == 0

    def test_unoffered_gap_refuses_before_the_command(self) -> None:
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_ticket_id": host.pk, "gap_keys": ["gap-a"]}]}
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
        ):
            attempt = self._record(proposals)

        assert "offered" in attempt.error
        attach.assert_not_called()
        assert TicketSweepRun.objects.count() == 0

    def test_retry_skips_a_gap_already_recorded_on_its_host(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        host = Ticket.objects.create(
            overlay="example",
            issue_url="https://github.com/example-org/example-repo/issues/2",
            extra={"dream_gap_batch": [{"gap_key": "gap-a"}]},
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_ticket_id": host.pk, "gap_keys": ["gap-a"]}]}
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
        ):
            attempt = self._record(proposals)

        assert attempt.error == ""
        attach.assert_not_called()
        assert TicketSweepRun.objects.get().finished_at is not None

    def test_owner_stop_records_no_run_or_proposals(self) -> None:
        result = {
            "summary": "needs input",
            "needs_user_input": True,
            "user_input_kind": "product_scope",
            "user_input_checked": ["The tracker was read."],
            "user_input_reason": "Should these issues be grouped?",
            "user_input_card": {
                "why": "The grouping needs your choice.",
                "blocker": "The target is unclear.",
                "options": [],
            },
            "sweep_proposals": {"examined": [], "folds": [], "gaps": []},
        }
        attempt = record_result_envelope(
            self.task, result, phase="backlog_sweep", usage=AttemptUsage(selected_harness="codex_app_server")
        )
        assert attempt.error == ""
        assert TicketSweepRun.objects.count() == 0
        self.ticket.refresh_from_db()
        assert "sweep_fold_proposals" not in self.ticket.extra
