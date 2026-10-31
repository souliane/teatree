"""A coding result for in-scope code is refused unless a revert-sweep receipt at HEAD lists no survivor."""

import subprocess
from pathlib import Path
from shutil import which

import pytest
from django.test import TestCase

from teatree.agents.attempt_recorder import record_result_envelope
from teatree.agents.sweep_report_gate import sweep_report_error
from teatree.core.models import Session, Task, Ticket, Worktree
from teatree.quality.revert_sweep import HunkStatus, HunkVerdict, RevertSweep, write_revert_sweep
from tests.teatree_core.models._shared import _init_repo_with_branch

IN_SCOPE_PATH = "src/teatree/core/gates/limit.py"
OUT_OF_SCOPE_PATH = "src/teatree/core/models/ticket.py"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        [which("git") or "git", "-c", "user.name=Test", "-c", "user.email=fixture.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


class TestSweepReportGate(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _claimed(self, *, phase: str = "coding") -> Task:
        ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.WORK_STARTED)
        session = Session.objects.create(ticket=ticket, agent_id=phase)
        task = Task.objects.create(ticket=ticket, session=session, phase=phase)
        task.claim(claimed_by="loop-slot")
        return task

    def _checkout(self, task: Task, *, changed: str) -> Path:
        repo = self._tmp_path / f"repo-{task.ticket.pk}"
        branch = f"feature-{task.ticket.pk}"
        _init_repo_with_branch(repo, branch=branch, commits_ahead=0)
        Worktree.objects.create(
            ticket=task.ticket, repo_path=str(repo), branch=branch, extra={"worktree_path": str(repo)}
        )
        self._commit(repo, changed)
        return repo

    def _commit(self, repo: Path, relative: str) -> str:
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{path.read_text() if path.exists() else ''}x = 1\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", f"edit {relative}")
        return _git(repo, "rev-parse", "HEAD")

    def _receipt(self, repo: Path, *statuses: HunkStatus) -> None:
        verdicts = tuple(HunkVerdict(IN_SCOPE_PATH, 2 + index, status) for index, status in enumerate(statuses))
        write_revert_sweep(repo, RevertSweep(_git(repo, "rev-parse", "HEAD"), verdicts))

    def test_in_scope_diff_without_a_receipt_is_refused(self) -> None:
        task = self._claimed()
        self._checkout(task, changed=IN_SCOPE_PATH)

        error = sweep_report_error(task)

        assert error.startswith("sweep_unpinned:")
        assert "no revert-sweep receipt" in error
        assert "t3 tool revert-sweep" in error

    def test_stale_receipt_is_refused(self) -> None:
        task = self._claimed()
        repo = self._checkout(task, changed=IN_SCOPE_PATH)
        self._receipt(repo, HunkStatus.KILLED)
        self._commit(repo, OUT_OF_SCOPE_PATH)

        error = sweep_report_error(task)

        assert error.startswith("sweep_unpinned:")
        assert "stale" in error

    def test_survivor_names_its_hunk_and_is_refused(self) -> None:
        task = self._claimed()
        repo = self._checkout(task, changed=IN_SCOPE_PATH)
        self._receipt(repo, HunkStatus.KILLED, HunkStatus.SURVIVOR)

        error = sweep_report_error(task)

        assert error.startswith("sweep_unpinned:")
        assert f"{IN_SCOPE_PATH}:3" in error

    def test_unmeasured_hunk_is_refused(self) -> None:
        task = self._claimed()
        repo = self._checkout(task, changed=IN_SCOPE_PATH)
        self._receipt(repo, HunkStatus.UNMEASURED)

        assert sweep_report_error(task).startswith("sweep_unpinned:")

    def test_clean_receipt_passes(self) -> None:
        task = self._claimed()
        repo = self._checkout(task, changed=IN_SCOPE_PATH)
        self._receipt(repo, HunkStatus.KILLED, HunkStatus.KILLED)

        assert sweep_report_error(task) == ""

    def test_out_of_scope_diff_is_untouched(self) -> None:
        task = self._claimed()
        self._checkout(task, changed=OUT_OF_SCOPE_PATH)

        assert sweep_report_error(task) == ""

    def test_phases_other_than_coding_and_debugging_are_untouched(self) -> None:
        task = self._claimed(phase="reviewing")
        self._checkout(task, changed=IN_SCOPE_PATH)

        assert sweep_report_error(task) == ""

    def test_task_without_a_checkout_is_untouched(self) -> None:
        assert sweep_report_error(self._claimed()) == ""

    def test_recorder_fails_a_coding_result_whose_receipt_lists_a_survivor(self) -> None:
        task = self._claimed()
        repo = self._checkout(task, changed=IN_SCOPE_PATH)
        self._receipt(repo, HunkStatus.SURVIVOR)

        attempt = record_result_envelope(
            task, {"summary": "done", "files_modified": [{"path": IN_SCOPE_PATH, "action": "modified"}]}
        )

        task.refresh_from_db()
        assert attempt.error.startswith("sweep_unpinned:")
        assert task.status == Task.Status.FAILED

    def test_recorder_completes_a_coding_result_with_a_clean_receipt(self) -> None:
        task = self._claimed()
        repo = self._checkout(task, changed=IN_SCOPE_PATH)
        self._receipt(repo, HunkStatus.KILLED)

        attempt = record_result_envelope(
            task, {"summary": "done", "files_modified": [{"path": IN_SCOPE_PATH, "action": "modified"}]}
        )

        task.refresh_from_db()
        assert attempt.error == ""
        assert task.status == Task.Status.COMPLETED
