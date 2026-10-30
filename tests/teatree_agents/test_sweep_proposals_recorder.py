"""The worker owns every write following a read-only sweep proposal."""

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase

from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope
from teatree.agents.sweep_proposals_recorder import _owner_citation, record_returned_sweep_proposals
from teatree.core.modelkit.question_card import internal_shorthand
from teatree.core.models import DeferredQuestion, Session, Task, Ticket, TicketSweepRun
from teatree.core.models.dream_gap_ledger import dream_umbrella_url
from teatree.core.models.task_handoff import resume_on_answer
from teatree.core.owner_question_message import render_blocks


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
        self.ticket.merge_extra(
            set_keys={
                "sweep_dispatch_window": {
                    "task_id": self.task.pk,
                    "index": index_urls if index_urls is not None else expected,
                    "examined": expected,
                    "external_count": 0,
                }
            }
        )
        with (
            patch("teatree.agents.sweep_proposals_recorder.get_overlay_for_ticket", return_value="overlay"),
            patch(
                "teatree.agents.sweep_proposals_recorder.owned_repo_slugs", return_value=("example-org/example-repo",)
            ),
            patch("teatree.agents.sweep_proposals_recorder.get_backend_provider", return_value=provider),
        ):
            return record_result_envelope(
                self.task,
                {"summary": "classified", "sweep_proposals": proposals},
                phase="backlog_sweep",
                usage=AttemptUsage(selected_harness=harness),
            )

    def _pending_gaps(self, *entries: dict[str, object]) -> Ticket:
        return Ticket.objects.create(
            overlay="example", issue_url=dream_umbrella_url(), extra={"dream_gap_pending": list(entries)}
        )

    def test_empty_executor_proposals_finish_a_measured_zero_run(self) -> None:
        attempt = self._record({"examined": [], "folds": [], "gaps": []})

        run = TicketSweepRun.objects.get()
        assert attempt.error == ""
        assert run.source == "loop"
        assert run.finished_at is not None
        assert run.examined_count == 0
        assert run.changed_count == 0

    def test_owned_repo_hook_failure_is_a_recorded_refusal(self) -> None:
        with (
            patch("teatree.agents.sweep_proposals_recorder.get_overlay_for_ticket", return_value="overlay"),
            patch("teatree.agents.sweep_proposals_recorder.owned_repo_slugs", side_effect=RuntimeError("offline")),
        ):
            refusal = record_returned_sweep_proposals(
                self.task,
                {"sweep_proposals": {"examined": [], "folds": [], "gaps": []}},
                phase="backlog_sweep",
            )

        assert refusal == "sweep_proposals_refused: the overlay could not be read (RuntimeError)"
        assert TicketSweepRun.objects.count() == 0

    def test_later_sweep_preserves_earlier_proposal_record(self) -> None:
        proposals = {"examined": [], "folds": [], "gaps": []}
        self._record(proposals)
        first_task_id = self.task.pk
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket),
            phase="backlog_sweep",
            execution_reason="ASK-GATE: return proposals",
        )
        self.task.claim(claimed_by="loop-slot")

        self._record(proposals)

        self.ticket.refresh_from_db()
        assert set(self.ticket.extra["sweep_fold_proposals"]) == {str(first_task_id), str(self.task.pk)}

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
        question = DeferredQuestion.objects.get()
        assert member in question.question or member in question.evidence.get("quoted", "")
        assert host in question.question or host in question.evidence.get("quoted", "")
        assert "same module" in question.evidence.get("quoted", "") or "same module" in question.evidence.get("why", "")
        assert question.parked_task_id is None
        answered = question.apply_answer("Approve", resolved_via=DeferredQuestion.ResolvedVia.SLACK)
        assert answered is not None
        assert resume_on_answer(answered, answered.parked_task) is None
        assert Task.objects.filter(parent_task=self.task).count() == 0
        assert TicketSweepRun.objects.get().examined_count == 2

    def test_real_citations_are_kept_in_record_and_safe_in_owner_card(self) -> None:
        host = "https://github.com/example-org/example-repo/issues/2"
        members = [f"https://github.com/example-org/example-repo/issues/{number}" for number in (1, 3, 4)]
        citations = [
            "Shipped in merged PR #123",
            "Covered by tests/foo.py and src/foo.py via snake_case --flag <!channel>",
            "same module " * 30 + "PR #124",
        ]
        proposals = {
            "examined": members,
            "folds": [
                {"member_url": fold_member, "host_url": host, "citation": citation}
                for fold_member, citation in zip(members, citations, strict=True)
            ],
            "gaps": [],
        }
        with patch(
            "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
        ):
            attempt = self._record(proposals, index_urls=[*members, host])

        self.ticket.refresh_from_db()
        assert attempt.error == "", attempt.error
        assert [
            fold["citation"] for fold in self.ticket.extra["sweep_fold_proposals"][str(self.task.pk)]["folds"]
        ] == citations
        quotes = [question.evidence["quoted"] for question in DeferredQuestion.objects.order_by("pk")]
        assert len(quotes) == 3
        assert all(not internal_shorthand(quote) for quote in quotes)
        assert all("<!channel>" not in quote for quote in quotes)
        assert quotes[2].endswith("...")
        assert all(
            "<!channel>" not in json.dumps(render_blocks(question)) for question in DeferredQuestion.objects.all()
        )

    def test_owner_citation_does_not_cut_an_html_entity(self) -> None:
        citation = _owner_citation("&" * 100)

        assert citation.endswith("...")
        assert citation.count("&") == citation.count("&amp;")

    def test_repeated_targets_are_verified_once_per_recording(self) -> None:
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/3"
        )
        self._pending_gaps({"gap_key": "gap-a"})
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        members = [f"https://github.com/example-org/example-repo/issues/{n}" for n in (1, 2)]
        proposals = {
            "examined": members,
            "folds": [
                {"member_url": member, "host_url": host.issue_url, "citation": "same work"} for member in members
            ],
            "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}],
        }
        verify = Mock(return_value={"state": "open"})
        with (
            patch("teatree.agents.sweep_proposals_recorder.require_self_authored_issue", verify),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed"),
        ):
            attempt = self._record(proposals, index_urls=[*members, host.issue_url])

        assert attempt.error == ""
        assert verify.call_count == 3

    def test_recording_does_not_reread_a_changed_issue_listing(self) -> None:
        self.ticket.merge_extra(
            set_keys={
                "sweep_dispatch_window": {"task_id": self.task.pk, "index": [], "examined": [], "external_count": 0}
            }
        )
        attempt = self._record({"examined": [], "folds": [], "gaps": []})

        assert attempt.error == "", attempt.error

    def test_duplicate_examined_url_refuses_a_window(self) -> None:
        url = "https://github.com/example-org/example-repo/issues/1"
        attempt = self._record({"examined": [url, url], "folds": [], "gaps": []}, expected_examined=[url])

        assert "examined" in attempt.error
        assert TicketSweepRun.objects.count() == 0

    def test_conflicting_fold_graph_refuses_before_asking(self) -> None:
        urls = [f"https://github.com/example-org/example-repo/issues/{number}" for number in (1, 2, 3)]
        for folds in (
            [(urls[0], urls[1]), (urls[0], urls[2])],
            [(urls[0], urls[1]), (urls[1], urls[0])],
        ):
            proposals = {
                "examined": urls,
                "folds": [{"member_url": member, "host_url": host, "citation": "same work"} for member, host in folds],
                "gaps": [],
            }
            with patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ):
                attempt = self._record(proposals)

            assert "fold" in attempt.error
            assert DeferredQuestion.objects.count() == 0
            assert TicketSweepRun.objects.count() == 0

    def test_examined_window_accepts_any_order(self) -> None:
        urls = [f"https://github.com/example-org/example-repo/issues/{number}" for number in (1, 2)]
        attempt = self._record({"examined": urls[::-1], "folds": [], "gaps": []}, expected_examined=urls)

        assert attempt.error == "", attempt.error
        assert TicketSweepRun.objects.get().examined_count == 2

    def test_examined_window_accepts_the_forge_url_casing(self) -> None:
        url = "https://github.com/Example-Org/Example-Repo/issues/1"

        attempt = self._record({"examined": [url], "folds": [], "gaps": []})

        assert attempt.error == "", attempt.error

    def test_completed_task_skips_validation_on_retry(self) -> None:
        proposals = {"examined": [], "folds": [], "gaps": []}
        assert self._record(proposals).error == ""
        error = record_returned_sweep_proposals(
            self.task,
            {"sweep_proposals": {"examined": ["invalid"], "folds": [], "gaps": []}},
            phase="backlog_sweep",
        )

        assert error == ""
        assert TicketSweepRun.objects.count() == 1

    def test_question_failure_leaves_no_idempotency_marker(self) -> None:
        member = "https://github.com/example-org/example-repo/issues/1"
        host = "https://github.com/example-org/example-repo/issues/2"
        proposals = {
            "examined": [member],
            "folds": [{"member_url": member, "host_url": host, "citation": "same module"}],
            "gaps": [],
        }
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch(
                "teatree.agents.sweep_proposals_recorder.DeferredQuestion.record",
                side_effect=RuntimeError("question unavailable"),
            ),
        ):
            attempt = self._record(proposals, index_urls=[member, host])

        self.ticket.refresh_from_db()
        assert "question unavailable" in attempt.error
        assert str(self.task.pk) not in self.ticket.extra.get("sweep_fold_proposals", {})
        assert TicketSweepRun.objects.incomplete().count() == 1
        assert TicketSweepRun.objects.recent().count() == 0

    def test_fold_question_failure_precedes_gap_forge_write(self) -> None:
        member = "https://github.com/example-org/example-repo/issues/1"
        self._pending_gaps({"gap_key": "gap-a"})
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        proposals = {
            "examined": [member],
            "folds": [{"member_url": member, "host_url": host.issue_url, "citation": "same module"}],
            "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}],
        }
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch(
                "teatree.agents.sweep_proposals_recorder.DeferredQuestion.record",
                side_effect=RuntimeError("unavailable"),
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
        ):
            attempt = self._record(proposals, index_urls=[member, host.issue_url])

        assert "unavailable" in attempt.error
        attach.assert_not_called()
        assert TicketSweepRun.objects.incomplete().count() == 1

    def test_attach_gap_refusal_includes_command_reason(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        self._pending_gaps({"gap_key": "gap-a"})
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}]}

        def refuse(*_args: object, stream: object, **_kwargs: object) -> None:
            stream.write("attach-gaps refused: host changed")
            raise SystemExit(1)

        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed", side_effect=refuse),
        ):
            attempt = self._record(proposals, index_urls=[host.issue_url])

        assert "host changed" in attempt.error
        assert TicketSweepRun.objects.incomplete().count() == 1
        assert TicketSweepRun.objects.recent().count() == 0

    def test_later_task_finishes_a_partially_attached_run_without_losing_its_count(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        self._pending_gaps({"gap_key": "gap-a"})
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}]}

        def attach_then_fail(*_args: object, **kwargs: object) -> None:
            run_id = _args[_args.index("--sweep-run-id") + 1]
            TicketSweepRun.objects.record_change(run_id=run_id, issue_url=host.issue_url)
            raise SystemExit(1)

        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed", side_effect=attach_then_fail),
        ):
            first = self._record(proposals, index_urls=[host.issue_url])
        assert first.error
        assert TicketSweepRun.objects.incomplete().count() == 1

        self.task = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket),
            phase="backlog_sweep",
            execution_reason="ASK-GATE: return proposals",
        )
        self.task.claim(claimed_by="loop-slot")
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch("teatree.agents.sweep_proposals_recorder.batch_entries", return_value=[{"gap_key": "gap-a"}]),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
        ):
            second = self._record({"examined": [], "folds": [], "gaps": []})

        assert second.error == "", second.error
        attach.assert_not_called()
        assert TicketSweepRun.objects.incomplete().count() == 0
        assert [run.changed_count for run in TicketSweepRun.objects.recent()] == [0, 1]

    def test_a_persistent_old_attach_refusal_does_not_block_later_sweeps(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        self._pending_gaps({"gap_key": "gap-a"})
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}]}

        def refuse(*_args: object, stream: object, **_kwargs: object) -> None:
            stream.write("attach-gaps refused: host changed")
            raise SystemExit(1)

        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed", side_effect=refuse),
        ):
            first = self._record(proposals, index_urls=[host.issue_url])
            self.task = Task.objects.create(
                ticket=self.ticket,
                session=Session.objects.create(ticket=self.ticket),
                phase="backlog_sweep",
                execution_reason="ASK-GATE: return proposals",
            )
            self.task.claim(claimed_by="loop-slot")
            second = self._record({"examined": [], "folds": [], "gaps": []})
        self.task = Task.objects.create(
            ticket=self.ticket,
            session=Session.objects.create(ticket=self.ticket),
            phase="backlog_sweep",
            execution_reason="ASK-GATE: return proposals",
        )
        self.task.claim(claimed_by="loop-slot")
        third = self._record({"examined": [], "folds": [], "gaps": []})

        assert "host changed" in first.error
        assert second.error == ""
        assert third.error == ""
        assert TicketSweepRun.objects.incomplete().count() == 1
        assert TicketSweepRun.objects.recent().count() == 2
        self.ticket.refresh_from_db()
        assert "sweep_pending_run" not in self.ticket.extra

    def test_gap_policy_refuses_before_a_run_or_question(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        umbrella = self._pending_gaps({"gap_key": "gap-a"})
        proposals = {"examined": [], "folds": [], "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}]}
        cases = (
            (Ticket.State.MERGED, {}, [{"gap_key": "gap-a"}], "settled"),
            (
                Ticket.State.NOT_STARTED,
                {"dream_gap_batch": [{"gap_key": f"old-{n}"} for n in range(3)]},
                [{"gap_key": "gap-a"}],
                "undispositioned",
            ),
            (Ticket.State.NOT_STARTED, {}, [{"gap_key": "gap-a", "declined_hosts": [host.pk]}], "declined"),
            (Ticket.State.NOT_STARTED, {}, [], "pending"),
        )
        for state, extra, pending, refusal in cases:
            with self.subTest(refusal=refusal):
                host.state = state
                host.extra = extra
                host.save(update_fields=["state", "extra"])
                umbrella.merge_extra(set_keys={"dream_gap_pending": pending})
                with (
                    patch(
                        "teatree.agents.sweep_proposals_recorder.require_self_authored_issue",
                        return_value={"state": "open"},
                    ),
                    patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
                ):
                    attempt = self._record(proposals, index_urls=[host.issue_url])
                assert refusal in attempt.error
                attach.assert_not_called()
                assert TicketSweepRun.objects.count() == 0
                assert DeferredQuestion.objects.count() == 0

    def test_duplicate_gap_assignments_refuse_before_a_run(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        self._pending_gaps({"gap_key": "gap-a"})
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {
            "examined": [],
            "folds": [],
            "gaps": [
                {"host_url": host.issue_url, "gap_keys": ["gap-a"]},
                {"host_url": host.issue_url, "gap_keys": ["gap-a"]},
            ],
        }
        with patch(
            "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
        ):
            attempt = self._record(proposals, index_urls=[host.issue_url])

        assert "more than once" in attempt.error
        assert TicketSweepRun.objects.count() == 0

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

    def test_closed_fold_target_refuses_before_any_write(self) -> None:
        member = "https://github.com/example-org/example-repo/issues/1"
        host = "https://github.com/example-org/example-repo/issues/2"
        proposals = {
            "examined": [member],
            "folds": [{"member_url": member, "host_url": host, "citation": "same work"}],
            "gaps": [],
        }
        with patch(
            "teatree.agents.sweep_proposals_recorder.require_self_authored_issue",
            side_effect=lambda **kwargs: {"state": "closed" if kwargs["issue_url"] == host else "open"},
        ):
            attempt = self._record(proposals, index_urls=[member, host])

        assert "not open" in attempt.error
        assert TicketSweepRun.objects.count() == 0
        assert DeferredQuestion.objects.count() == 0

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
        self._pending_gaps({"gap_key": "gap-a"})
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}]}
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue",
                return_value={"state": "open"},
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
        ):
            attempt = self._record(proposals, index_urls=[host.issue_url])

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
            "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}],
        }
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
        ):
            attempt = self._record(proposals, index_urls=[host.issue_url])

        assert attempt.error
        attach.assert_not_called()
        assert TicketSweepRun.objects.count() == 0

    def test_unoffered_gap_refuses_before_the_command(self) -> None:
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}]}
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
        ):
            attempt = self._record(proposals, index_urls=[host.issue_url])

        assert "key not offered in this sweep" in attempt.error
        attach.assert_not_called()
        assert TicketSweepRun.objects.count() == 0

    def test_gap_host_outside_the_index_refuses_before_the_command(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        self._pending_gaps({"gap_key": "gap-a"})
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}]}
        with patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach:
            attempt = self._record(proposals)

        assert "no valid host" in attempt.error
        attach.assert_not_called()
        assert TicketSweepRun.objects.count() == 0

    def test_gap_host_in_another_overlay_refuses_before_the_command(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        self._pending_gaps({"gap_key": "gap-a"})
        host = Ticket.objects.create(overlay="other", issue_url="https://github.com/example-org/example-repo/issues/2")
        proposals = {"examined": [], "folds": [], "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}]}
        with patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach:
            attempt = self._record(proposals, index_urls=[host.issue_url])

        assert "not an owned ticket" in attempt.error
        attach.assert_not_called()
        assert TicketSweepRun.objects.count() == 0

    def test_offered_gap_without_an_umbrella_refuses_before_a_run(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        host = Ticket.objects.create(
            overlay="example", issue_url="https://github.com/example-org/example-repo/issues/2"
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}]}
        with patch(
            "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
        ):
            attempt = self._record(proposals, index_urls=[host.issue_url])

        assert "no umbrella ticket" in attempt.error
        assert TicketSweepRun.objects.count() == 0

    def test_retry_skips_a_gap_already_recorded_on_its_host(self) -> None:
        self.task.execution_reason += "\nDream promotion batch (#4776)\n- gap-a: offered work"
        host = Ticket.objects.create(
            overlay="example",
            issue_url="https://github.com/example-org/example-repo/issues/2",
            state=Ticket.State.MERGED,
            extra={"dream_gap_batch": [{"gap_key": "gap-a"}]},
        )
        proposals = {"examined": [], "folds": [], "gaps": [{"host_url": host.issue_url, "gap_keys": ["gap-a"]}]}
        with (
            patch(
                "teatree.agents.sweep_proposals_recorder.require_self_authored_issue", return_value={"state": "open"}
            ),
            patch("teatree.agents.sweep_proposals_recorder.call_command_streamed") as attach,
        ):
            attempt = self._record(proposals, index_urls=[host.issue_url])

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
