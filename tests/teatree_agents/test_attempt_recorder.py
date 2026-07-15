"""Shared result-envelope recorder used by both dispatch backends."""

from pathlib import Path
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import (
    AttemptUsage,
    ResultEnvelopeError,
    parse_result_envelope,
    record_result_envelope,
    validate_result_keys,
)
from teatree.core.models import (
    DeferredQuestion,
    Directive,
    DirectiveDispatch,
    PendingArticleSuggestion,
    Session,
    Task,
    TaskAttempt,
    Ticket,
    Worktree,
)
from teatree.verification.url_check import UrlCheckResult, UrlCheckStatus
from tests.teatree_core.models._shared import _init_repo_with_branch


def _valid_sketch(**overrides: object) -> dict[str, object]:
    sketch: dict[str, object] = {
        "kind": "setting_policy_gate",
        "setting_key": "max_open_prs_per_repo_per_ticket",
        "setting_type": "int",
        "neutral_default": 0,
        "policy_chokepoint": "src/teatree/core/gates/pr_budget_gate.py::check_pr_budget",
        "activation_scope": "t3-teatree",
        "activation_value": 1,
        "rejected_alternatives": ["an overlay-local hook — fails N=2"],
        "acceptance_tests": ["tests/teatree_core/gates/test_pr_budget_gate.py::TestCheckPrBudget"],
    }
    sketch.update(overrides)
    return sketch


class TestParseResultEnvelope(TestCase):
    def test_parses_object(self) -> None:
        assert parse_result_envelope('{"summary": "ok"}') == {"summary": "ok"}

    def test_rejects_non_object(self) -> None:
        with pytest.raises(ResultEnvelopeError):
            parse_result_envelope("[1, 2]")

    def test_rejects_invalid_json(self) -> None:
        with pytest.raises(ResultEnvelopeError):
            parse_result_envelope("not json")


class TestValidateResultKeys(TestCase):
    def test_accepts_schema_keys(self) -> None:
        assert validate_result_keys({"summary": "x", "tests_passed": 3}) == ""

    def test_rejects_unknown_keys(self) -> None:
        assert "unexpected keys" in validate_result_keys({"bogus": 1})


class TestRecordResultEnvelope(TestCase):
    def _claimed(self, *, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def test_outage_death_fails_task_without_advancing_ticket(self) -> None:
        task = self._claimed()
        attempt = record_result_envelope(
            task,
            {"summary": "Unable to connect to API", "files_modified": [{"path": "a.py", "action": "modified"}]},
        )
        task.refresh_from_db()
        task.ticket.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.ticket.state == Ticket.State.STARTED
        assert attempt.result == {}
        assert attempt.error.startswith("outage_death:")

    def test_outage_death_takes_precedence_over_evidence_gate(self) -> None:
        task = self._claimed()
        record_result_envelope(task, {"summary": "API Error: connection refused"})
        task.refresh_from_db()
        latest = task.attempts.order_by("-pk").first()
        assert task.status == Task.Status.FAILED
        assert latest is not None
        assert latest.error.startswith("outage_death:")

    def test_success_completes_and_stamps_usage(self) -> None:
        task = self._claimed()
        attempt = record_result_envelope(
            task,
            {"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]},
            usage=AttemptUsage(agent_session_id="sess", cost_usd=0.4, num_turns=3),
        )
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert attempt.cost_usd == pytest.approx(0.4)
        assert attempt.num_turns == 3
        assert attempt.agent_session_id == "sess"

    def test_success_stamps_lane_when_supplied(self) -> None:
        task = self._claimed()
        attempt = record_result_envelope(
            task,
            {"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]},
            usage=AttemptUsage(lane=TaskAttempt.Lane.METERED),
        )
        assert attempt.lane == "metered"

    def test_lane_defaults_to_blank_when_not_supplied(self) -> None:
        task = self._claimed()
        attempt = record_result_envelope(
            task,
            {"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]},
        )
        assert attempt.lane == ""

    def test_evidence_gate_fails_task(self) -> None:
        task = self._claimed()
        record_result_envelope(task, {"summary": "nothing changed"})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_unexpected_keys_fail_task(self) -> None:
        task = self._claimed()
        record_result_envelope(task, {"summary": "x", "bogus": True})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED


class TestLandingVerifiedCompletion(TestCase):
    """A coding result completes only when a commit actually landed (root-cause gate)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _claimed(self, *, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def _attach_worktree(self, ticket: Ticket, *, commits_ahead: int) -> Path:
        repo_dir = self._tmp_path / f"repo-{ticket.pk}"
        branch = f"feature-{ticket.pk}"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=commits_ahead)
        Worktree.objects.create(
            ticket=ticket,
            repo_path=str(repo_dir),
            branch=branch,
            extra={"worktree_path": str(repo_dir)},
        )
        return repo_dir

    def test_files_modified_without_commit_is_refused(self) -> None:
        task = self._claimed()
        self._attach_worktree(task.ticket, commits_ahead=0)
        record_result_envelope(
            task,
            {"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]},
        )
        task.refresh_from_db()
        task.ticket.refresh_from_db()
        latest = task.attempts.order_by("-pk").first()
        assert task.status == Task.Status.FAILED
        assert task.ticket.state == Ticket.State.STARTED  # FSM did NOT advance
        assert latest is not None
        assert latest.error.startswith("landing_unverified:")

    def test_files_modified_with_real_commit_completes(self) -> None:
        task = self._claimed()
        self._attach_worktree(task.ticket, commits_ahead=1)
        record_result_envelope(
            task,
            {"summary": "done", "files_modified": [{"path": "f0.txt", "action": "created"}]},
        )
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED


class TestScanningNewsEnvelopeChannel(TestCase):
    """A shell-denied scanning_news agent hands candidates back through the envelope (#9)."""

    def _claimed(self) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id="scanning_news")
        task = Task.objects.create(ticket=ticket, session=session, phase="scanning_news")
        task.claim(claimed_by="loop-slot")
        return task

    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_article_suggestions_round_trip_to_pending_rows(self, check_url: object) -> None:
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        task = self._claimed()
        record_result_envelope(
            task,
            {
                "summary": "2 candidates",
                "article_suggestions": [
                    {"title": "A", "url": "https://ex.com/a", "rationale": "why a"},
                    {"title": "B", "url": "https://ex.com/b", "rationale": "why b"},
                ],
            },
        )
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        rows = PendingArticleSuggestion.objects.all()
        assert rows.count() == 2
        assert set(rows.values_list("url", flat=True)) == {"https://ex.com/a", "https://ex.com/b"}
        assert {row.overlay for row in rows} == {"acme"}
        assert {row.status for row in rows} == {PendingArticleSuggestion.Status.PENDING}

    def test_summary_only_scanning_news_is_refused(self) -> None:
        task = self._claimed()
        record_result_envelope(task, {"summary": "nothing found today"})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert PendingArticleSuggestion.objects.count() == 0

    def test_url_less_suggestions_fail_the_task_persisting_nothing(self) -> None:
        # The gate must refuse a nonempty-but-url-less hand-back the recorder
        # would drop entirely — not complete the task over zero persisted rows.
        task = self._claimed()
        record_result_envelope(task, {"summary": "found 1", "article_suggestions": [{"title": "no url"}]})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert PendingArticleSuggestion.objects.count() == 0


class TestAnsweringEnvelopeChannel(TestCase):
    """A shell-denied answering agent hands its draft back for approval-gated posting (#9)."""

    def _claimed(self) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id="answering")
        task = Task.objects.create(ticket=ticket, session=session, phase="answering")
        task.claim(claimed_by="loop-slot")
        return task

    def test_answer_draft_routes_to_a_deferred_question(self) -> None:
        task = self._claimed()
        record_result_envelope(
            task,
            {"summary": "drafted", "answer": {"text": "Here is the reply.", "thread_ref": "C123/168.9"}},
        )
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        question = DeferredQuestion.objects.get()
        assert question.parked_task_id == task.pk
        assert "Here is the reply." in question.question
        assert "C123/168.9" in question.question
        assert question.is_pending

    def test_summary_only_answering_is_refused(self) -> None:
        task = self._claimed()
        record_result_envelope(task, {"summary": "drafted but not returned"})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.count() == 0

    def test_text_less_answer_fails_the_task_persisting_nothing(self) -> None:
        # A draft with only a thread_ref persists no DeferredQuestion, so the gate
        # must refuse it rather than complete over a dropped reply.
        task = self._claimed()
        record_result_envelope(task, {"summary": "drafted", "answer": {"thread_ref": "C1/1.0"}})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert DeferredQuestion.objects.count() == 0


class TestDirectiveInterpretationEnvelopeChannel(TestCase):
    """A shell-denied directive interpreter hands its sketch back through the envelope (PR-6)."""

    def _dispatched_task(self) -> tuple[Directive, Task]:
        directive = Directive.objects.capture("max 1 MR per repo for overlay X", source=Directive.Source.CLI)
        row = DirectiveDispatch.enqueue(directive=directive, contract="c")
        assert row is not None
        assert row.task is not None
        return directive, row.task

    def test_a_returned_sketch_completes_the_task_and_records_it(self) -> None:
        directive, task = self._dispatched_task()
        record_result_envelope(
            task,
            {
                "summary": "interpreted",
                "directive_interpretation": {"constraint_statement": "at most 1 open PR", "sketch": _valid_sketch()},
            },
            phase="directive_interpreting",
        )
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        directive.refresh_from_db()
        assert directive.state == Directive.State.INTERPRETED
        assert directive.sketch is not None

    def test_an_invalid_sketch_fails_the_task_recording_no_garbage(self) -> None:
        directive, task = self._dispatched_task()
        record_result_envelope(
            task,
            {"summary": "x", "directive_interpretation": {"sketch": _valid_sketch(rejected_alternatives=[])}},
            phase="directive_interpreting",
        )
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        directive.refresh_from_db()
        assert directive.state == Directive.State.CAPTURED
