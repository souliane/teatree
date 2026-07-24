"""Shared result-envelope recorder used by both dispatch backends."""

from pathlib import Path
from unittest.mock import MagicMock, patch

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
    PendingChatInjection,
    PendingTriageRecommendation,
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
        blob = {"summary": "Unable to connect to API", "files_modified": [{"path": "a.py", "action": "modified"}]}
        attempt = record_result_envelope(task, blob)
        task.refresh_from_db()
        task.ticket.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.ticket.state == Ticket.State.STARTED
        # The offending blob is persisted on the FAILED attempt for debuggability,
        # not discarded (PR-3): a failure the operator cannot inspect is a dead end.
        assert attempt.result == blob
        assert attempt.error.startswith("outage_death:")

    def test_failed_attempt_persists_the_offending_result_blob(self) -> None:
        task = self._claimed()
        blob = {"summary": "nothing changed"}  # coding evidence refusal, no files_modified
        attempt = record_result_envelope(task, blob)
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert attempt.result == blob

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

    def test_success_stamps_reasoning_effort_and_skills(self) -> None:
        task = self._claimed()
        attempt = record_result_envelope(
            task,
            {"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]},
            usage=AttemptUsage(reasoning_effort="xhigh", skills_loaded=["t3:code", "t3:rules"]),
        )
        assert attempt.reasoning_effort == "xhigh"
        assert attempt.skills_loaded == ["t3:code", "t3:rules"]

    def test_reasoning_effort_and_skills_default_empty(self) -> None:
        task = self._claimed()
        attempt = record_result_envelope(
            task,
            {"summary": "done", "files_modified": [{"path": "a.py", "action": "modified"}]},
        )
        assert attempt.reasoning_effort == ""
        assert attempt.skills_loaded == []

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

    def _attach_worktree(self, ticket: Ticket, *, commits_ahead: int, suffix: str = "") -> Path:
        repo_dir = self._tmp_path / f"repo-{ticket.pk}{suffix}"
        branch = f"feature-{ticket.pk}{suffix}"
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

    def test_evidence_refusal_with_committed_work_is_salvaged_and_completed(self) -> None:
        # #3263: the coder committed real work but omitted the files_modified
        # envelope. Rather than refuse and strand the branch, the recorder
        # synthesizes files_modified from the committed diff and COMPLETES.
        task = self._claimed()
        self._attach_worktree(task.ticket, commits_ahead=1)
        attempt = record_result_envelope(task, {"summary": "implemented but forgot the envelope"})
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        salvaged = attempt.result.get("files_modified")
        assert salvaged == [{"path": "f0.txt", "action": "modified"}]

    def test_evidence_refusal_without_commit_still_fails(self) -> None:
        # No committed work to salvage — the refusal stands (the transient
        # requeue sweep then gives the bounded corrective retry / escalates).
        task = self._claimed()
        self._attach_worktree(task.ticket, commits_ahead=0)
        record_result_envelope(task, {"summary": "did nothing, no envelope"})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_evidence_refusal_with_dirty_uncommitted_work_is_not_salvaged(self) -> None:
        # A commit exists but tracked changes are uncommitted: landing would
        # refuse, so there is nothing clean to salvage — the task fails.
        task = self._claimed()
        repo_dir = self._attach_worktree(task.ticket, commits_ahead=1)
        (repo_dir / "f0.txt").write_text("edited but not committed\n")
        record_result_envelope(task, {"summary": "half done"})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED

    def test_salvage_skips_commitless_worktree_and_uses_the_committed_one(self) -> None:
        # A ticket with two clean worktrees, only one carrying a commit: the
        # salvage skips the commit-less one and synthesizes from the committed one.
        task = self._claimed()
        self._attach_worktree(task.ticket, commits_ahead=0)
        self._attach_worktree(task.ticket, commits_ahead=1, suffix="b")
        attempt = record_result_envelope(task, {"summary": "committed in one repo, no envelope"})
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert attempt.result.get("files_modified") == [{"path": "f0.txt", "action": "modified"}]

    def test_salvage_fails_closed_when_the_diff_read_errors(self) -> None:
        # A commit landed (landing passes) but the name-only diff read errors —
        # the salvage yields nothing rather than completing on unknowable work.
        task = self._claimed()
        self._attach_worktree(task.ticket, commits_ahead=1)
        with patch("teatree.agents.attempt_recorder.git.run", side_effect=OSError("boom")):
            record_result_envelope(task, {"summary": "committed but diff read broke"})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED


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

    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_recorded_batch_surfaces_one_owner_approval_dm(self, check_url: object) -> None:
        # The shell-denied agent cannot post the approval DM itself, so the server
        # DMs ONE owner-audience batch listing the candidates it just recorded.
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        task = self._claimed()
        record_result_envelope(
            task,
            {
                "summary": "1 candidate",
                "article_suggestions": [{"title": "Agent evals", "url": "https://ex.com/a", "rationale": "why"}],
            },
        )
        question = DeferredQuestion.objects.get()
        assert question.is_pending
        assert question.audience == DeferredQuestion.Audience.OWNER_QUESTION
        assert question.parked_task_id is None
        assert "https://ex.com/a" in question.question

    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_no_new_candidates_posts_no_dm(self, check_url: object) -> None:
        # A re-scan that records zero NEW rows (all deduped) must not re-nag.
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        blob = {"summary": "1", "article_suggestions": [{"title": "A", "url": "https://ex.com/a", "rationale": "w"}]}
        record_result_envelope(self._claimed(), blob)
        DeferredQuestion.objects.all().delete()
        record_result_envelope(self._claimed(), blob)  # same URL → deduped, zero new rows
        assert DeferredQuestion.objects.count() == 0

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


class TestTriageAssessingEnvelopeChannel(TestCase):
    """A shell-denied triage_assessing agent hands recommendations back through the envelope (#9)."""

    def _claimed(self) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id="triage_assessing")
        task = Task.objects.create(ticket=ticket, session=session, phase="triage_assessing")
        task.claim(claimed_by="loop-slot")
        return task

    def test_recommendations_round_trip_to_pending_rows_and_one_question(self) -> None:
        task = self._claimed()
        record_result_envelope(
            task,
            {
                "summary": "2 assessed",
                "triage_recommendations": [
                    {"issue_url": "https://ex.com/1", "verdict": "close", "rationale": "dupe"},
                    {"issue_url": "https://ex.com/2", "verdict": "keep", "rationale": "still valid"},
                ],
            },
        )
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        rows = PendingTriageRecommendation.objects.all()
        assert rows.count() == 2
        assert set(rows.values_list("issue_url", flat=True)) == {"https://ex.com/1", "https://ex.com/2"}
        assert {row.overlay for row in rows} == {"acme"}
        assert {row.status for row in rows} == {PendingTriageRecommendation.Status.PENDING}
        # Exactly ONE DeferredQuestion DMs the batch, parked to the task.
        question = DeferredQuestion.objects.get()
        assert question.parked_task_id == task.pk
        assert question.is_pending

    def test_summary_only_triage_assessing_is_refused(self) -> None:
        task = self._claimed()
        record_result_envelope(task, {"summary": "assessed nothing"})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert PendingTriageRecommendation.objects.count() == 0
        assert DeferredQuestion.objects.count() == 0

    def test_url_less_recommendations_fail_the_task_persisting_nothing(self) -> None:
        task = self._claimed()
        record_result_envelope(task, {"summary": "one", "triage_recommendations": [{"verdict": "close"}]})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert PendingTriageRecommendation.objects.count() == 0
        assert DeferredQuestion.objects.count() == 0

    def test_unknown_verdict_recommendations_fail_the_task_persisting_nothing(self) -> None:
        # The recorder drops an unknown verdict fail-closed; the gate matches, so a
        # hand-back the recorder would drop entirely fails the task, not completes it.
        task = self._claimed()
        record_result_envelope(
            task, {"summary": "one", "triage_recommendations": [{"issue_url": "https://ex.com/9", "verdict": "nuke"}]}
        )
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert PendingTriageRecommendation.objects.count() == 0
        assert DeferredQuestion.objects.count() == 0


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


class TestOwnerDmAnsweringRepliesInThread(TestCase):
    """An owner-DM answering task SENDS the reply in-thread, never the defer gate.

    The owner's #1 complaint: an inbound owner DM in ``autonomous_away`` got a
    "Approve this drafted reply?" pending question parked instead of an answer.
    Answering the owner is not a post on the owner's behalf, so it must never
    route through the away/approval defer gate — the reply is posted directly,
    threaded under the owner's own message, regardless of availability.
    """

    def _owner_dm_task(self, *, channel: str = "D0OWNER", slack_ts: str = "1784474278.074869") -> Task:
        ticket = Ticket.objects.create(
            role=Ticket.Role.AUTHOR,
            state=Ticket.State.STARTED,
            overlay="acme",
            extra={"slack_answer": {"channel": channel, "slack_ts": slack_ts, "question": "test"}},
        )
        session = Session.objects.create(ticket=ticket, agent_id="answering")
        task = Task.objects.create(ticket=ticket, session=session, phase="answering")
        task.claim(claimed_by="loop-slot")
        return task

    def test_owner_dm_reply_is_posted_in_thread_not_deferred(self) -> None:
        channel, slack_ts = "D0OWNER", "1784474278.074869"
        task = self._owner_dm_task(channel=channel, slack_ts=slack_ts)
        PendingChatInjection.objects.create(overlay="acme", channel=channel, slack_ts=slack_ts, text="test")
        backend = MagicMock()
        backend.post_reply.return_value = {"ok": True, "ts": "1784475031.606029"}
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend):
            record_result_envelope(
                task,
                {"summary": "drafted", "answer": {"text": "Got it, working.", "thread_ref": ""}},
            )
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        # Sent directly, threaded under the OWNER's message ts (authoritative
        # ticket coordinates, not the advisory agent-returned thread_ref).
        backend.post_reply.assert_called_once_with(channel=channel, ts=slack_ts, text="Got it, working.")
        # Never parked behind the away/approval defer gate.
        assert DeferredQuestion.objects.count() == 0
        # The owner-question row is stamped answered so the turn-end gate rests.
        assert PendingChatInjection.objects.get().answered_at is not None

    def test_failed_post_falls_back_to_the_approval_gate_losing_nothing(self) -> None:
        task = self._owner_dm_task()
        backend = MagicMock()
        backend.post_reply.return_value = {"ok": False, "error": "channel_not_found"}
        with patch("teatree.core.backend_factory.messaging_from_overlay", return_value=backend):
            record_result_envelope(
                task,
                {"summary": "drafted", "answer": {"text": "Got it.", "thread_ref": ""}},
            )
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        # A Slack failure must not drop the reply: it falls back to the on-behalf gate.
        assert DeferredQuestion.objects.get().parked_task_id == task.pk

    def test_non_owner_answering_still_uses_the_approval_gate(self) -> None:
        # No slack_answer context (an on-behalf colleague/channel reply): the
        # approval gate is unchanged.
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED, overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id="answering")
        task = Task.objects.create(ticket=ticket, session=session, phase="answering")
        task.claim(claimed_by="loop-slot")
        with patch("teatree.core.backend_factory.messaging_from_overlay") as resolve:
            record_result_envelope(
                task,
                {"summary": "drafted", "answer": {"text": "On behalf reply.", "thread_ref": "C9/1.0"}},
            )
            resolve.assert_not_called()
        assert DeferredQuestion.objects.get().parked_task_id == task.pk


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
