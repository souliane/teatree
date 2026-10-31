"""A coding result with no new commit lands only when it proves the branch already holds the fix."""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.envelope_refusal import MALFORMED_FIX_RECORD_PREFIX
from teatree.agents.landing_verification import landing_verification_error
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.utils import git
from tests.teatree_core.conftest import record_executed_repro_for_test
from tests.teatree_core.models._shared import _init_repo_with_branch


class TestAlreadyFixedNoCommitEnding(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _task_on_unadvanced_branch(self, *, kind: Ticket.Kind = Ticket.Kind.FEATURE) -> tuple[Task, str]:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, kind=kind)
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id="coding"), phase="coding"
        )
        repo_dir = self._tmp_path / f"repo-{ticket.pk}"
        branch = f"feature-{ticket.pk}"
        _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=0)
        Worktree.objects.create(
            ticket=ticket, repo_path=str(repo_dir), branch=branch, extra={"worktree_path": str(repo_dir)}
        )
        return task, git.head_sha(repo=str(repo_dir))

    @staticmethod
    def _result(verified_head: str, evidence: str) -> dict[str, object]:
        return {"fix_record": {"already_fixed": {"verified_head": verified_head, "evidence": evidence}}}

    def test_a_claim_verified_at_the_worktree_head_with_evidence_lands(self) -> None:
        task, head = self._task_on_unadvanced_branch()
        result = self._result(head, "tests/test_x.py::test_y passes at this head")
        assert landing_verification_error(task, result=result) == ""

    def test_a_claim_verified_at_another_head_is_refused(self) -> None:
        task, _head = self._task_on_unadvanced_branch()
        result = self._result("0" * 40, "tests/test_x.py::test_y passes")
        assert landing_verification_error(task, result=result).startswith("landing_unverified:")

    def test_a_claim_without_evidence_is_refused(self) -> None:
        task, head = self._task_on_unadvanced_branch()
        assert landing_verification_error(task, result=self._result(head, "  ")).startswith("landing_unverified:")

    def test_a_result_with_no_claim_is_still_refused(self) -> None:
        task, _head = self._task_on_unadvanced_branch()
        assert landing_verification_error(task, result={"fix_record": {}}).startswith("landing_unverified:")
        assert landing_verification_error(task).startswith("landing_unverified:")


class TestAlreadyFixedThroughTheRecorder(TestCase):
    """The claim-only hand-back the re-read clause prescribes completes the task through ``record_result_envelope``."""

    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _claimed_task(self, *, kind: Ticket.Kind = Ticket.Kind.FEATURE, on_disk: bool = True) -> tuple[Task, str]:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED, kind=kind)
        if kind == Ticket.Kind.FIX:
            record_executed_repro_for_test(ticket)
        task = Task.objects.create(
            ticket=ticket, session=Session.objects.create(ticket=ticket, agent_id="coding"), phase="coding"
        )
        head = ""
        if on_disk:
            repo_dir = self._tmp_path / f"repo-{ticket.pk}"
            branch = f"feature-{ticket.pk}"
            _init_repo_with_branch(repo_dir, branch=branch, commits_ahead=0)
            Worktree.objects.create(
                ticket=ticket, repo_path=str(repo_dir), branch=branch, extra={"worktree_path": str(repo_dir)}
            )
            head = git.head_sha(repo=str(repo_dir))
        task.claim(claimed_by="loop-slot")
        return task, head

    @staticmethod
    def _claim_only(verified_head: str, evidence: str = "tests/test_x.py::test_y passes at this head") -> dict:
        return {
            "summary": "the branch already holds the fix",
            "fix_record": {"already_fixed": {"verified_head": verified_head, "evidence": evidence}},
        }

    def test_a_verified_claim_alone_completes_the_task_and_stores_no_record(self) -> None:
        task, head = self._claimed_task(kind=Ticket.Kind.FIX)
        attempt = record_result_envelope(task, self._claim_only(head))
        task.refresh_from_db()
        assert attempt.error == ""
        assert task.status == Task.Status.COMPLETED
        assert "fix_record" not in (task.ticket.extra or {})

    def test_a_claim_at_another_head_fails_the_task_as_unverified(self) -> None:
        task, _head = self._claimed_task()
        attempt = record_result_envelope(task, self._claim_only("0" * 40))
        task.refresh_from_db()
        assert attempt.error.startswith("landing_unverified:")
        assert task.status == Task.Status.FAILED

    def test_a_claim_with_no_worktree_to_check_it_against_fails_the_task(self) -> None:
        task, _head = self._claimed_task(on_disk=False)
        attempt = record_result_envelope(task, self._claim_only("a" * 40))
        task.refresh_from_db()
        assert attempt.error.startswith("landing_unverified:")
        assert task.status == Task.Status.FAILED

    def test_a_claim_without_evidence_does_not_stand_in_for_files_modified(self) -> None:
        task, head = self._claimed_task()
        attempt = record_result_envelope(task, self._claim_only(head, evidence="  "))
        task.refresh_from_db()
        assert attempt.error.startswith("missing required evidence")
        assert task.status == Task.Status.FAILED

    def test_a_partial_record_beside_the_claim_is_still_refused_by_name(self) -> None:
        task, head = self._claimed_task()
        result = self._claim_only(head)
        result["fix_record"]["root_cause"] = "x"
        attempt = record_result_envelope(task, result)
        assert attempt.error.startswith(MALFORMED_FIX_RECORD_PREFIX)
