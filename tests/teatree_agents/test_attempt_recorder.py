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
from teatree.agents.envelope_refusal import UNPARSEABLE_ENVELOPE_PREFIX
from teatree.agents.result_schema_owner_stop import owner_stop_limits_lines
from teatree.core.issue_hygiene import CreateOutcome
from teatree.core.modelkit.owner_decision import OWNER_RAISABLE_DECISIONS
from teatree.core.modelkit.task_failure_taxonomy import (
    OWNER_STOP_REFUSAL_PREFIX,
    PLAN_STALE_PREFIX,
    FailureKind,
    classify_failure,
)
from teatree.core.models import (
    DeferredQuestion,
    Directive,
    DirectiveDispatch,
    PendingArticleSuggestion,
    PendingChatInjection,
    PendingTriageRecommendation,
    PlanArtifact,
    Session,
    SessionCommand,
    Task,
    TaskAttempt,
    Ticket,
    Worktree,
)
from teatree.core.self_forge_identities import ExternalIssueRefusedError
from teatree.core.send_proxy import OutboundBlockedError
from teatree.loop.transient_requeue import requeue_transient_failed
from teatree.verification.url_check import UrlCheckResult, UrlCheckStatus
from tests._owner_channel import assert_a_plain_card, owner_stop
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

    def test_rejects_invalid_json_with_the_shared_unparseable_prefix(self) -> None:
        with pytest.raises(ResultEnvelopeError) as raised:
            parse_result_envelope("not json")

        assert str(raised.value).startswith(UNPARSEABLE_ENVELOPE_PREFIX)


DENIED_COMMAND = "The command was denied with a 403 and the sub-agent start failed."
DENIED_START = "The sub-agent start was denied by the current route."


def _routed() -> AttemptUsage:
    return AttemptUsage(route_candidate_index=0, route_source_skill="debug", selected_harness="codex_app_server")


class TestValidateResultKeys(TestCase):
    def test_accepts_schema_keys(self) -> None:
        assert validate_result_keys({"summary": "x", "tests_passed": 3}) == ""

    def test_rejects_unknown_keys(self) -> None:
        assert "unexpected keys" in validate_result_keys({"bogus": 1})

    def test_accepts_each_owner_decision_kind_with_a_complete_card(self) -> None:
        for kind in OWNER_RAISABLE_DECISIONS:
            assert validate_result_keys(owner_stop("Rotate the deploy token?", kind) | {"summary": "x"}) == ""

    def test_user_input_kind_without_checked_is_refused(self) -> None:
        stop = owner_stop("Rotate the deploy token?") | {"summary": "x"}
        for checked in ([], ["  "], "a"):
            error = validate_result_keys(stop | {"user_input_checked": checked})
            assert "user_input_checked" in error, checked

    def test_an_incomplete_card_is_refused_naming_each_gap(self) -> None:
        stop = owner_stop("Rotate the deploy token?") | {"summary": "x", "user_input_card": {}}

        error = validate_result_keys(stop)

        assert "the why sentence is required" in error
        assert "the blocker is required" in error
        assert "user_input_card" in error

    def test_an_owner_stop_over_120_words_is_refused(self) -> None:
        stop = owner_stop("Rotate the deploy token?") | {"summary": "x"}
        stop["user_input_card"] = {**stop["user_input_card"], "why": " ".join(["word"] * 110) + "."}

        error = validate_result_keys(stop)

        assert "words; at most 120" in error

    def test_internal_shorthand_in_an_owner_stop_is_refused(self) -> None:
        error = validate_result_keys(owner_stop("Is widget_count ready for ticket 104?") | {"summary": "x"})

        assert "widget_count" in error
        assert "ticket 104" in error
        assert error.startswith(OWNER_STOP_REFUSAL_PREFIX)
        assert all(line in error for line in owner_stop_limits_lines())

    def test_unknown_user_input_kind_is_refused(self) -> None:
        error = validate_result_keys(owner_stop("Rotate the deploy token?", "gate_refusal") | {"summary": "x"})
        assert "user_input_kind" in error
        assert "credentials" in error
        assert error.startswith(OWNER_STOP_REFUSAL_PREFIX)
        assert all(line in error for line in owner_stop_limits_lines())
        assert classify_failure(error) == FailureKind.RESULT_SCHEMA_INVALID

    def test_pushing_the_own_branch_or_stopping_the_own_process_is_refused_to_decide(self) -> None:
        for kind, question in (
            ("public_post", "May I push the finished branch once more after the push round limit?"),
            ("irreversible", "May I stop the dev server I started for this check?"),
        ):
            with self.subTest(kind=kind):
                error = validate_result_keys(owner_stop(question, kind) | {"summary": "waiting"})
                assert error.startswith(OWNER_STOP_REFUSAL_PREFIX)
                assert "standing factory rule already decides" in error

    def test_a_stop_no_row_proves_a_misfit_keeps_its_card(self) -> None:
        for kind, question, facts in (
            ("money_or_plan", "May I buy more credits after the round limit?", ()),
            ("money_or_plan", "May I spend money on this service?", ()),
            ("money_or_plan", "May I pay to extend the round limit?", ()),
            ("credentials", "May I continue after the round limit?", ()),
            ("credentials", "May I retry the blocked read-only verification through the approval router?", ()),
            (
                "credentials",
                "Can you provide bot access with write scope?",
                ("The existing approved credential is read-only.",),
            ),
            ("credentials", "May I grant the agent an admin token after the round limit?", ()),
            ("credentials", "May I widen the credential's use to a new service?", ()),
            ("irreversible", "May I delete work that exists on no remote?", ()),
            ("irreversible", "May I stop the server I started and delete its unpushed data?", ()),
            ("public_post", "May I send these words to a colleague in your name?", ()),
            ("public_post", "May I comment on a colleague's pull request?", ()),
            ("public_post", "May I push the finished branch and reply to the reviewer?", ()),
            ("public_post", "May I push to a colleague's branch?", ()),
        ):
            with self.subTest(kind=kind, question=question):
                assert validate_result_keys(owner_stop(question, kind, *facts) | {"summary": "waiting"}) == ""


class TestRecordResultEnvelope(TestCase):
    def _claimed(self, *, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def test_accepted_operator_directions_are_carried_into_the_handoff_result(self) -> None:
        task = self._claimed()
        SessionCommand.objects.create(
            command_id="accepted",
            ticket=task.ticket,
            task=task,
            text="Keep the public API",
            outcome="accepted_current_turn",
        )
        SessionCommand.objects.create(
            command_id="rejected", ticket=task.ticket, task=task, text="Ignore tests", outcome="rejected"
        )

        attempt = record_result_envelope(task, {"summary": "Work continues", "operator_inputs": ["untrusted"]})

        assert attempt.result["operator_inputs"] == ["Keep the public API"]

    def test_queued_operator_message_is_recorded_only_after_success(self) -> None:
        task = self._claimed(phase="testing")
        command = SessionCommand.objects.create(
            command_id="queued-result", ticket=task.ticket, task=task, text="Preserve the endpoint", outcome="queued"
        )

        attempt = record_result_envelope(
            task,
            {
                "summary": "Tested",
                "tests_run": [{"name": "focused", "passed": True}],
                "tests_passed": 1,
                "tests_failed": 0,
            },
        )

        command.refresh_from_db()
        assert attempt.result["operator_inputs"] == ["Preserve the endpoint"]
        assert command.outcome == "accepted_new_turn"

    def test_push_ownership_venue_fault_fails_without_an_owner_question(self) -> None:
        task = self._claimed(phase="debugging")
        attempt = record_result_envelope(
            task,
            {
                "summary": "Committed locally; the push gate could not authenticate.",
                "push_ownership_venue_fault": True,
                "needs_user_input": False,
                "files_modified": [{"path": "a.py", "action": "modified"}],
            },
            usage=AttemptUsage(route_candidate_index=0, route_source_skill="debug"),
        )

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert attempt.failure_kind == FailureKind.RESULT_ERROR
        assert task.attempts.failed_route_candidates("debug") == {0: attempt.pk}
        assert DeferredQuestion.objects.count() == 0

    def test_routed_credentials_denied_command_fails_this_candidate_without_an_owner_question(self) -> None:
        task = self._claimed(phase="debugging")
        envelope = owner_stop("May I start the sub-agent again?", "credentials", DENIED_START)

        attempt = record_result_envelope(
            task, envelope | {"summary": "Current route denied the start."}, usage=_routed()
        )

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert attempt.failure_kind == FailureKind.RESULT_ERROR
        assert attempt.error == "result_error: routed agent access denied"
        assert task.attempts.failed_route_candidates("debug") == {0: attempt.pk}
        assert DeferredQuestion.objects.count() == 0
        assert requeue_transient_failed() == 1
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING

    def test_unrouted_credentials_denied_command_still_raises_the_card(self) -> None:
        task = self._claimed(phase="scoping")
        envelope = owner_stop("May I start the sub-agent again?", "credentials", DENIED_START)

        attempt = record_result_envelope(task, envelope | {"summary": "waiting"}, usage=AttemptUsage())

        assert attempt.error == ""
        assert assert_a_plain_card(DeferredQuestion.objects.get(parked_task=task))

    def test_routed_credentials_stop_without_a_denial_word_keeps_its_card(self) -> None:
        task = self._claimed(phase="scoping")
        fact = "The sub-agent start returned a 403 after a timeout."
        envelope = owner_stop("May I start the sub-agent again?", "credentials", fact)

        attempt = record_result_envelope(task, envelope | {"summary": "waiting"}, usage=_routed())

        assert attempt.error == ""
        assert assert_a_plain_card(DeferredQuestion.objects.get(parked_task=task))

    def test_owner_kept_stops_record_a_card_on_routed_and_unrouted_runs(self) -> None:
        for kind, question, fact in (
            ("money_or_plan", "May I spend money on this service?", DENIED_COMMAND),
            ("irreversible", "May I delete work that exists on no remote?", DENIED_COMMAND),
            ("public_post", "May I send these words to a colleague in your name?", DENIED_COMMAND),
            ("credentials", "May I use an API token for the new service?", DENIED_COMMAND),
            ("credentials", "May I widen the credential's use to a new service?", "The request failed with a 403."),
        ):
            for usage in (_routed(), AttemptUsage()):
                with self.subTest(kind=kind, question=question, routed=usage.route_candidate_index is not None):
                    task = self._claimed(phase="scoping")

                    attempt = record_result_envelope(
                        task, owner_stop(question, kind, fact) | {"summary": "waiting"}, usage=usage
                    )

                    assert attempt.error == ""
                    card = DeferredQuestion.objects.get(parked_task=task)
                    assert card.evidence["decision"] == kind
                    assert_a_plain_card(card)

    def test_routed_coder_without_edits_records_each_owner_kept_card(self) -> None:
        for phase in ("coding", "debugging"):
            for kind, question in (
                ("money_or_plan", "May I spend money on this service?"),
                ("irreversible", "May I delete work that exists on no remote?"),
                ("public_post", "May I send these words to a colleague in your name?"),
                ("credentials", "May I use an API token for the new service?"),
            ):
                with self.subTest(phase=phase, kind=kind):
                    task = self._claimed(phase=phase)

                    attempt = record_result_envelope(
                        task, owner_stop(question, kind) | {"summary": "waiting"}, usage=_routed()
                    )

                    assert attempt.error == ""
                    assert_a_plain_card(DeferredQuestion.objects.get(parked_task=task))

    def test_routed_coder_without_edits_refuses_non_owner_kept_stop(self) -> None:
        for kind in ("architecture",):
            with self.subTest(kind=kind):
                task = self._claimed(phase="coding")

                attempt = record_result_envelope(
                    task, owner_stop("May I choose a new direction?", kind) | {"summary": "waiting"}, usage=_routed()
                )

                assert attempt.error.startswith("landing_unverified:")
                assert not DeferredQuestion.objects.filter(parked_task=task).exists()

    def test_refused_own_branch_push_fails_the_attempt_and_is_reopened_once(self) -> None:
        task = self._claimed(phase="debugging")
        question = "May I push the finished branch once more after the push round limit?"

        attempt = record_result_envelope(task, owner_stop(question, "public_post") | {"summary": "waiting"})

        task.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert attempt.failure_kind == FailureKind.RESULT_SCHEMA_INVALID
        assert attempt.error.startswith(OWNER_STOP_REFUSAL_PREFIX)
        assert "standing factory rule already decides" in attempt.error
        assert DeferredQuestion.objects.count() == 0
        assert requeue_transient_failed() == 1

    def test_quoted_push_gate_marker_does_not_fail_a_task(self) -> None:
        task = self._claimed(phase="testing")
        attempt = record_result_envelope(
            task,
            {
                "summary": "The log quoted venue_fault: push ownership gate had no usable GitHub credential",
                "tests_run": [{"name": "focused", "passed": True}],
                "tests_passed": 1,
                "tests_failed": 0,
            },
        )

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert attempt.failure_kind == ""

    def test_push_fault_field_outside_push_phase_does_not_fail_a_task(self) -> None:
        task = self._claimed(phase="testing")
        attempt = record_result_envelope(
            task,
            {
                "summary": "Tests passed",
                "push_ownership_venue_fault": True,
                "tests_run": [{"name": "focused", "passed": True}],
                "tests_passed": 1,
                "tests_failed": 0,
            },
        )

        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert attempt.failure_kind == ""

    def test_outage_death_fails_task_without_advancing_ticket(self) -> None:
        task = self._claimed()
        blob = {"summary": "Unable to connect to API", "files_modified": [{"path": "a.py", "action": "modified"}]}
        attempt = record_result_envelope(task, blob)
        task.refresh_from_db()
        task.ticket.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.ticket.state == Ticket.State.WORK_STARTED
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

    def test_failed_hand_record_keeps_its_source(self) -> None:
        task = self._claimed()
        attempt = record_result_envelope(
            task,
            {"summary": "nothing changed"},
            usage=AttemptUsage(recording_source="hand"),
        )
        assert attempt.result["recording_source"] == "hand"
        assert task.recorded_by_hand()


class TestLandingVerifiedCompletion(TestCase):
    """A coding result completes only when a commit actually landed (root-cause gate)."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _claimed(self, *, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
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

    def _attach_unverifiable_worktree(self, ticket: Ticket) -> Path:
        """Attach a checkout PRESENT on disk whose commit-count probe cannot answer.

        A real directory that is not a git repository reproduces the venue-split
        condition without a mock: ``git rev-list`` exits 128 there exactly as it
        does for a checkout whose admin dir was written by another execution
        context and is unreachable from this one.
        """
        repo_dir = self._tmp_path / f"unverifiable-{ticket.pk}"
        repo_dir.mkdir()
        Worktree.objects.create(
            ticket=ticket,
            repo_path=str(repo_dir),
            branch=f"unverifiable-{ticket.pk}",
            extra={"worktree_path": str(repo_dir)},
        )
        return repo_dir

    def test_unverifiable_sibling_worktree_still_completes_a_landed_run(self) -> None:
        # The recorder runs after the agent finished, so there is no tick left to
        # hold: an unprobeable sibling must not crash it and discard the run.
        task = self._claimed()
        self._attach_unverifiable_worktree(task.ticket)
        self._attach_worktree(task.ticket, commits_ahead=1)
        record_result_envelope(
            task,
            {"summary": "done", "files_modified": [{"path": "f0.txt", "action": "created"}]},
        )
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED

    def test_salvage_skips_an_unverifiable_worktree(self) -> None:
        task = self._claimed()
        self._attach_unverifiable_worktree(task.ticket)
        self._attach_worktree(task.ticket, commits_ahead=1)
        attempt = record_result_envelope(task, {"summary": "committed in one repo, no envelope"})
        task.refresh_from_db()
        assert task.status == Task.Status.COMPLETED
        assert attempt.result.get("files_modified") == [{"path": "f0.txt", "action": "modified"}]

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
        assert task.ticket.state == Ticket.State.WORK_STARTED  # FSM did NOT advance
        assert latest is not None
        assert latest.error.startswith("landing_unverified:")

    def test_routed_coder_with_existing_worktree_keeps_owner_card(self) -> None:
        task = self._claimed()
        self._attach_worktree(task.ticket, commits_ahead=0)

        attempt = record_result_envelope(
            task,
            owner_stop("May I spend money on this service?", "money_or_plan") | {"summary": "waiting"},
            usage=_routed(),
        )

        assert attempt.error == ""
        assert_a_plain_card(DeferredQuestion.objects.get(parked_task=task))

    def test_a_plan_refused_at_coded_fails_the_attempt_as_plan_stale_with_no_success_row(self) -> None:
        task = self._claimed()
        Ticket.objects.filter(pk=task.ticket_id).update(state=Ticket.State.PLAN_RECORDED)
        PlanArtifact.objects.create(ticket=task.ticket, plan_text="legacy", recorded_by="op")
        self._attach_worktree(task.ticket, commits_ahead=1)

        attempt = record_result_envelope(
            task,
            {"summary": "done", "files_modified": [{"path": "f0.txt", "action": "created"}]},
        )

        task.refresh_from_db()
        task.ticket.refresh_from_db()
        assert task.status == Task.Status.FAILED
        assert task.failure_kind == FailureKind.PLAN_STALE
        assert list(task.attempts.values_list("pk", flat=True)) == [attempt.pk]
        assert attempt.error.startswith(PLAN_STALE_PREFIX)
        assert task.ticket.state == Ticket.State.PLAN_RECORDED

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
        with patch("teatree.agents.coding_result_salvage.git.run", side_effect=OSError("boom")):
            record_result_envelope(task, {"summary": "committed but diff read broke"})
        task.refresh_from_db()
        assert task.status == Task.Status.FAILED


def _news_file_candidate(**overrides: object) -> dict[str, object]:
    candidate: dict[str, object] = {
        "title": "Agent evals",
        "url": "https://ex.com/a",
        "rationale": "Improve eval coverage",
        "action": "file",
        "backlog_checked": True,
        "base_red": False,
        "duplicate_of": "",
        "ticket_title": "Add agent eval coverage",
        "ticket_body": "The agent eval coverage is missing this pattern.",
    }
    return candidate | overrides


class TestScanningNewsEnvelopeChannel(TestCase):
    """A shell-denied scanning_news agent hands candidates back through the envelope (#9)."""

    def _claimed(self) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id="scanning_news")
        task = Task.objects.create(ticket=ticket, session=session, phase="scanning_news")
        task.claim(claimed_by="loop-slot")
        return task

    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_article_suggestions_round_trip_to_settled_rows(self, check_url: object) -> None:
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
        assert {row.status for row in rows} == {PendingArticleSuggestion.Status.DROPPED}

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.agents.reactive_envelope_recorders.create_or_extend_by_marker")
    @patch("teatree.agents.reactive_envelope_recorders._resolved_code_host")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_news_scan_files_fit_candidate_and_settles_row(
        self, check_url: MagicMock, code_host: MagicMock, create_issue: MagicMock, notify: MagicMock
    ) -> None:
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        create_issue.return_value = CreateOutcome(
            kind="created_new", issue_url="https://github.com/souliane/teatree/issues/9000"
        )
        code_host.return_value.get_issue.side_effect = lambda _: {"body": create_issue.call_args.kwargs["draft"].body}
        record_result_envelope(
            self._claimed(),
            {
                "summary": "one fitting idea",
                "article_suggestions": [_news_file_candidate()],
            },
        )
        row = PendingArticleSuggestion.objects.get()
        assert row.status == PendingArticleSuggestion.Status.FILED
        assert row.issue_url == "https://github.com/souliane/teatree/issues/9000"
        assert row.decided_at is not None
        assert DeferredQuestion.objects.count() == 0
        assert create_issue.call_count == 1
        assert create_issue.call_args.kwargs["draft"].labels == ("from-news-scan", "needs-triage")
        code_host.assert_called_once_with("acme")
        notify.assert_called_once()

    @patch("teatree.agents.reactive_envelope_recorders.create_or_extend_by_marker")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_news_scan_drops_red_base_without_filing_or_card(
        self, check_url: MagicMock, create_issue: MagicMock
    ) -> None:
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        record_result_envelope(
            self._claimed(),
            {
                "summary": "base already red",
                "article_suggestions": [_news_file_candidate(base_red=True)],
            },
        )
        row = PendingArticleSuggestion.objects.get()
        assert row.status == PendingArticleSuggestion.Status.DROPPED
        assert row.decided_at is not None
        assert DeferredQuestion.objects.count() == 0
        create_issue.assert_not_called()

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.agents.reactive_envelope_recorders.append_description_section")
    @patch("teatree.agents.reactive_envelope_recorders.open_candidates")
    @patch("teatree.agents.reactive_envelope_recorders._resolved_code_host")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_news_scan_extends_a_confirmed_open_duplicate(
        self, check_url: MagicMock, code_host: MagicMock, open_issues: MagicMock, append: MagicMock, notify: MagicMock
    ) -> None:
        target = "https://github.com/souliane/teatree/issues/9001"
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        open_issues.return_value = {target: {"html_url": target}}
        record_result_envelope(
            self._claimed(),
            {"summary": "one duplicate", "article_suggestions": [_news_file_candidate(duplicate_of=target)]},
        )
        row = PendingArticleSuggestion.objects.get()
        assert row.status == PendingArticleSuggestion.Status.FILED
        assert row.issue_url == target
        assert append.call_args.kwargs["issue_url"] == target
        assert append.call_args.kwargs["section"].action == "news_scan_candidate"
        code_host.assert_called_once_with("acme")
        notify.assert_called_once()

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.agents.reactive_envelope_recorders.create_or_extend_by_marker")
    @patch("teatree.agents.reactive_envelope_recorders._resolved_code_host")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_two_articles_for_one_idea_share_the_issue_marker(
        self, check_url: MagicMock, code_host: MagicMock, create_issue: MagicMock, notify: MagicMock
    ) -> None:
        issue_url = "https://github.com/souliane/teatree/issues/9000"
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        create_issue.side_effect = [
            CreateOutcome(kind="created_new", issue_url=issue_url),
            CreateOutcome(kind="extended_existing", issue_url=issue_url),
        ]
        code_host.return_value.get_issue.side_effect = lambda _: {"body": create_issue.call_args.kwargs["draft"].body}
        record_result_envelope(
            self._claimed(),
            {
                "summary": "one idea from two articles",
                "article_suggestions": [
                    _news_file_candidate(url="https://ex.com/a"),
                    _news_file_candidate(url="https://ex.com/b", ticket_title="  ADD agent   eval coverage "),
                ],
            },
        )
        rows = list(PendingArticleSuggestion.objects.all())
        assert len(rows) == 2
        assert {row.issue_url for row in rows} == {issue_url}
        assert create_issue.call_count == 2
        first, second = create_issue.call_args_list
        assert first.kwargs["marker"] == second.kwargs["marker"]
        code_host.assert_called()
        notify.assert_called_once()

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.agents.reactive_envelope_recorders.append_description_section")
    @patch("teatree.agents.reactive_envelope_recorders.open_candidates")
    @patch("teatree.agents.reactive_envelope_recorders._resolved_code_host")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_unknown_open_duplicate_drops_without_creating_a_copy(
        self, check_url: MagicMock, code_host: MagicMock, open_issues: MagicMock, append: MagicMock, notify: MagicMock
    ) -> None:
        target = "https://github.com/souliane/teatree/issues/9001"
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        open_issues.return_value = {}
        record_result_envelope(
            self._claimed(),
            {"summary": "stale duplicate", "article_suggestions": [_news_file_candidate(duplicate_of=target)]},
        )
        row = PendingArticleSuggestion.objects.get()
        assert row.status == PendingArticleSuggestion.Status.DROPPED
        assert "no longer open" in row.decision_reason
        append.assert_not_called()
        code_host.assert_called_once_with("acme")
        notify.assert_called_once()

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.agents.reactive_envelope_recorders.append_description_section")
    @patch("teatree.agents.reactive_envelope_recorders.open_candidates")
    @patch("teatree.agents.reactive_envelope_recorders._resolved_code_host")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_external_duplicate_is_recorded_without_editing_or_copying_it(
        self, check_url: MagicMock, code_host: MagicMock, open_issues: MagicMock, append: MagicMock, notify: MagicMock
    ) -> None:
        target = "https://github.com/souliane/teatree/issues/9001"
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        open_issues.return_value = {target: {"html_url": target}}
        append.side_effect = ExternalIssueRefusedError(target, "other", "not self-authored")
        record_result_envelope(
            self._claimed(),
            {"summary": "external duplicate", "article_suggestions": [_news_file_candidate(duplicate_of=target)]},
        )
        row = PendingArticleSuggestion.objects.get()
        assert row.status == PendingArticleSuggestion.Status.DROPPED
        assert target in row.decision_reason
        code_host.assert_called_once_with("acme")
        notify.assert_called_once()

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.agents.reactive_envelope_recorders.create_or_extend_by_marker")
    @patch("teatree.agents.reactive_envelope_recorders._resolved_code_host")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_external_marker_conflict_is_dropped_without_a_second_issue(
        self, check_url: MagicMock, code_host: MagicMock, create_issue: MagicMock, notify: MagicMock
    ) -> None:
        target = "https://github.com/souliane/teatree/issues/9001"
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        create_issue.return_value = CreateOutcome(kind="external_conflict", issue_url=target)
        record_result_envelope(
            self._claimed(),
            {"summary": "external issue", "article_suggestions": [_news_file_candidate()]},
        )
        row = PendingArticleSuggestion.objects.get()
        assert row.status == PendingArticleSuggestion.Status.DROPPED
        assert target in row.decision_reason
        code_host.return_value.get_issue.assert_not_called()
        notify.assert_called_once()

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.agents.reactive_envelope_recorders.create_or_extend_by_marker")
    @patch("teatree.agents.reactive_envelope_recorders._resolved_code_host")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_missing_forge_issue_url_rolls_back_candidate(
        self, check_url: MagicMock, code_host: MagicMock, create_issue: MagicMock, notify: MagicMock
    ) -> None:
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        create_issue.return_value = CreateOutcome(kind="created_new")
        with pytest.raises(RuntimeError, match="no issue URL"):
            record_result_envelope(
                self._claimed(), {"summary": "no URL", "article_suggestions": [_news_file_candidate()]}
            )
        assert PendingArticleSuggestion.objects.count() == 0
        code_host.assert_called_once_with("acme")
        notify.assert_not_called()

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.agents.reactive_envelope_recorders.create_or_extend_by_marker")
    @patch("teatree.agents.reactive_envelope_recorders._resolved_code_host")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_news_scan_rolls_back_candidate_when_filing_fails(
        self, check_url: MagicMock, code_host: MagicMock, create_issue: MagicMock, notify: MagicMock
    ) -> None:
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        for failure in (RuntimeError("forge unavailable"), OutboundBlockedError("privacy refused")):
            with self.subTest(failure=type(failure).__name__):
                create_issue.side_effect = failure
                with pytest.raises(type(failure), match=str(failure)):
                    record_result_envelope(
                        self._claimed(),
                        {"summary": "retry needed", "article_suggestions": [_news_file_candidate()]},
                    )
                assert PendingArticleSuggestion.objects.count() == 0
        notify.assert_not_called()
        assert code_host.call_count == 2

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.agents.reactive_envelope_recorders.create_or_extend_by_marker")
    @patch("teatree.agents.reactive_envelope_recorders._resolved_code_host")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_news_scan_refuses_an_unconfirmed_forge_write(
        self, check_url: MagicMock, code_host: MagicMock, create_issue: MagicMock, notify: MagicMock
    ) -> None:
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        create_issue.return_value = CreateOutcome(
            kind="created_new", issue_url="https://github.com/souliane/teatree/issues/9000"
        )
        code_host.return_value.get_issue.return_value = {"body": "stale issue body"}
        with pytest.raises(RuntimeError, match="could not be confirmed"):
            record_result_envelope(
                self._claimed(),
                {"summary": "unconfirmed", "article_suggestions": [_news_file_candidate()]},
            )
        assert PendingArticleSuggestion.objects.count() == 0
        notify.assert_not_called()

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_news_scan_drops_incomplete_filing_evidence(self, check_url: MagicMock, notify: MagicMock) -> None:
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        cases = (
            ("unchecked", {"backlog_checked": False}, "backlog"),
            ("unknown-base", {"base_red": None}, "base"),
            ("no-draft", {"ticket_body": ""}, "draft"),
            ("declined", {"action": "drop", "decision_reason": "no scoped benefit"}, "no scoped benefit"),
        )
        for suffix, overrides, reason in cases:
            with self.subTest(suffix=suffix):
                item = _news_file_candidate(url=f"https://ex.com/{suffix}", **overrides)
                record_result_envelope(self._claimed(), {"summary": "settled", "article_suggestions": [item]})
                row = PendingArticleSuggestion.objects.get(url=item["url"])
                assert row.status == PendingArticleSuggestion.Status.DROPPED
                assert reason in row.decision_reason
        assert notify.call_count == len(cases)

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_new_candidate_records_settled_row_and_one_digest_without_question(
        self, check_url: MagicMock, notify: MagicMock
    ) -> None:
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        task = self._claimed()
        record_result_envelope(
            task,
            {
                "summary": "1 candidate",
                "article_suggestions": [{"title": "Agent evals", "url": "https://ex.com/a", "rationale": "why"}],
            },
        )
        row = PendingArticleSuggestion.objects.get()
        assert row.status == PendingArticleSuggestion.Status.DROPPED
        assert row.url == "https://ex.com/a"
        assert DeferredQuestion.objects.count() == 0
        notify.assert_called_once()
        assert notify.call_args.kwargs["idempotency_key"] == f"news-digest-{task.pk}"

    @patch("teatree.agents.reactive_envelope_recorders.notify_user")
    @patch("teatree.core.models.pending_article_suggestion.check_url")
    def test_later_scan_of_same_url_records_no_new_row_or_digest(self, check_url: MagicMock, notify: MagicMock) -> None:
        check_url.return_value = UrlCheckResult(url="", status=UrlCheckStatus.OK, http_status=200)
        blob = {"summary": "1", "article_suggestions": [{"title": "A", "url": "https://ex.com/a", "rationale": "w"}]}
        record_result_envelope(self._claimed(), blob)
        record_result_envelope(self._claimed(), blob)
        assert PendingArticleSuggestion.objects.count() == 1
        assert DeferredQuestion.objects.count() == 0
        notify.assert_called_once()

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
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, overlay="acme")
        session = Session.objects.create(ticket=ticket, agent_id="triage_assessing")
        task = Task.objects.create(ticket=ticket, session=session, phase="triage_assessing")
        task.claim(claimed_by="loop-slot")
        return task

    def test_recommendations_round_trip_to_pending_rows_without_an_owner_question(self) -> None:
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
        assert not DeferredQuestion.objects.exists()

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
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, overlay="acme")
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
        assert question.is_pending
        assert question.evidence["decision"] == "public_post"
        text = assert_a_plain_card(question, "C123/168.9")
        assert "> Here is the reply." in text
        assert text.index("[Post it] (recommended)") < text.index("[Do not post]")

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
            state=Ticket.State.WORK_STARTED,
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
        # The owner-question row records that the agent personally replied.
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
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, overlay="acme")
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
