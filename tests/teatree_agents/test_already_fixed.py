"""A coding result with no new commit lands only when it proves the branch already holds the fix."""

from pathlib import Path

import pytest
from django.test import TestCase

from teatree.agents.landing_verification import landing_verification_error
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.utils import git
from tests.teatree_core.models._shared import _init_repo_with_branch


class TestAlreadyFixedNoCommitEnding(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _task_on_unadvanced_branch(self) -> tuple[Task, str]:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
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
