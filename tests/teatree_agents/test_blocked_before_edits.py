"""A routed or non-claude_sdk coder that reports it is blocked, having changed nothing, is a refusal (#5152)."""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import AttemptUsage, record_result_envelope
from teatree.core.models import Session, Task, Ticket, Worktree
from tests.teatree_core.models._shared import _init_repo_with_branch

_BLOCKED = {"summary": "Blocked before edits: the fetch failed", "needs_user_input": True, "user_input_reason": "auth"}
_CODEX = AttemptUsage(selected_harness="codex_app_server", route_candidate_index=0, route_source_skill="code")
_ROUTED_CLAUDE = AttemptUsage(selected_harness="claude_sdk", route_candidate_index=1, route_source_skill="code")
_UNROUTED_CLAUDE = AttemptUsage(selected_harness="claude_sdk")
_UNROUTED_CODEX = AttemptUsage(selected_harness="codex_app_server")


class TestBlockedBeforeEdits(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _claimed(self, phase: str) -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id=phase), phase=phase
        )
        task.claim(claimed_by="loop-slot")
        return task

    def _attach_worktree(self, ticket: Ticket, *, commits_ahead: int) -> None:
        repo_dir = self._tmp_path / f"repo-{ticket.pk}"
        branch = f"feature-{ticket.pk}"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=commits_ahead)
        Worktree.objects.create(
            ticket=ticket, repo_path="org/repo", branch=branch, extra={"worktree_path": str(repo_dir)}
        )

    def test_a_routed_or_non_claude_result_with_no_edits_is_refused_and_the_ticket_stays_put(self) -> None:
        for phase in ("coding", "debugging"):
            for usage in (_CODEX, _ROUTED_CLAUDE, _UNROUTED_CODEX):
                with self.subTest(phase=phase, harness=usage.selected_harness, index=usage.route_candidate_index):
                    task = self._claimed(phase)

                    attempt = record_result_envelope(task, dict(_BLOCKED), phase=phase, usage=usage)

                    task.refresh_from_db()
                    task.ticket.refresh_from_db()
                    assert attempt.error.startswith("landing_unverified: blocked before edits")
                    assert (attempt.outcome, attempt.failure_kind) == ("refusal", "landing_unverified")
                    assert task.status == Task.Status.FAILED
                    assert task.ticket.state == Ticket.State.WORK_STARTED

    def _attach_unverifiable_worktree(self, ticket: Ticket) -> None:
        checkout = self._tmp_path / f"unverifiable-{ticket.pk}"
        checkout.mkdir()
        Worktree.objects.create(ticket=ticket, repo_path="org/repo", branch="b", extra={"worktree_path": str(checkout)})

    def test_a_worktree_whose_commit_check_cannot_answer_does_not_get_the_hand_off_refused(self) -> None:
        task = self._claimed("coding")
        self._attach_unverifiable_worktree(task.ticket)

        attempt = record_result_envelope(task, dict(_BLOCKED), phase="coding", usage=_CODEX)

        assert attempt.outcome == "success"

    def test_the_same_result_from_an_unrouted_claude_dispatch_still_completes(self) -> None:
        task = self._claimed("coding")

        attempt = record_result_envelope(task, dict(_BLOCKED), phase="coding", usage=_UNROUTED_CLAUDE)

        task.refresh_from_db()
        assert (attempt.outcome, task.status) == ("success", Task.Status.COMPLETED)

    def test_a_worktree_with_no_new_commit_gets_the_blocked_diagnosis(self) -> None:
        task = self._claimed("coding")
        self._attach_worktree(task.ticket, commits_ahead=0)

        attempt = record_result_envelope(task, dict(_BLOCKED), phase="coding", usage=_CODEX)

        assert attempt.error.startswith("landing_unverified: blocked before edits")

    def test_a_hand_off_after_commits_landed_is_not_refused(self) -> None:
        task = self._claimed("coding")
        self._attach_worktree(task.ticket, commits_ahead=1)

        attempt = record_result_envelope(task, dict(_BLOCKED), phase="coding", usage=_CODEX)

        assert attempt.outcome == "success"

    def test_a_result_that_lists_the_files_it_changed_is_not_a_blocked_one(self) -> None:
        task = self._claimed("coding")
        result = {**_BLOCKED, "files_modified": [{"path": "a.py", "action": "modified"}]}

        attempt = record_result_envelope(task, result, phase="coding", usage=_CODEX)

        assert attempt.outcome == "success"

    def test_a_phase_that_does_not_land_commits_is_not_judged_by_it(self) -> None:
        task = self._claimed("planning")

        attempt = record_result_envelope(task, dict(_BLOCKED), phase="planning", usage=_CODEX)

        assert attempt.outcome == "success"
