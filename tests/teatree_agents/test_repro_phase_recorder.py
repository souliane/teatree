from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.agents.repro_phase_recorder import record_phase_repro
from teatree.core.models import ConfigSetting, HarnessRun, ReproEvidence, Session, Task, Ticket, Worktree
from tests.teatree_core.models._shared import _init_repo_with_branch


class TestRecordPhaseReproSwitch(TestCase):
    @pytest.fixture(autouse=True)
    def _inject_tmp_path(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def _task(self, phase: str) -> Task:
        ticket = Ticket.objects.create(overlay="acme", kind=Ticket.Kind.FIX)
        session = Session.objects.create(ticket=ticket, agent_id="tester")
        return Task.objects.create(ticket=ticket, session=session, phase=phase)

    def test_the_default_still_refuses_coding_without_a_red(self) -> None:
        assert "repro record-red" in record_phase_repro(self._task("coding"), phase="coding")

    def test_switched_off_coding_completion_is_not_refused(self) -> None:
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False, scope="acme")
        task = self._task("coding")
        with self.assertLogs("teatree.core.gates.repro_gate", level="WARNING") as logs:
            assert record_phase_repro(task, phase="coding") == ""
        assert f"ticket {task.ticket_id}" in logs.output[0]
        assert "forced_repro_gate_enabled" in logs.output[0]
        assert "acme" in logs.output[0]

    def test_switched_off_testing_completion_runs_no_green_replay(self) -> None:
        ConfigSetting.objects.set_value("forced_repro_gate_enabled", value=False, scope="acme")
        task = self._task("testing")
        repo_dir = self._tmp_path / "replay-repo"
        _init_repo_with_branch(repo_dir, branch="feature", commits_ahead=1)
        Worktree.objects.create(
            ticket=task.ticket, repo_path=str(repo_dir), branch="feature", extra={"worktree_path": str(repo_dir)}
        )
        red = ReproEvidence.record_red(
            ticket=task.ticket,
            command="false",
            run=HarnessRun(head_sha="a" * 40, exit_code=1, output="failed"),
        )
        with patch(
            "teatree.agents.repro_phase_recorder.run_bounded_group",
            return_value=SimpleNamespace(returncode=1, stdout="failed", stderr=""),
        ) as replay:
            result = record_phase_repro(task, phase="testing")
        replay.assert_not_called()
        assert result == ""
        red.refresh_from_db()
        assert red.green_exit_code is None

    def test_enabled_testing_completion_reaches_green_replay(self) -> None:
        task = self._task("testing")
        repo_dir = self._tmp_path / "replay-repo"
        _init_repo_with_branch(repo_dir, branch="feature", commits_ahead=1)
        Worktree.objects.create(
            ticket=task.ticket, repo_path=str(repo_dir), branch="feature", extra={"worktree_path": str(repo_dir)}
        )
        ReproEvidence.record_red(
            ticket=task.ticket,
            command="false",
            run=HarnessRun(head_sha="a" * 40, exit_code=1, output="failed"),
        )
        with patch("teatree.agents.repro_phase_recorder.run_bounded_group") as replay:
            replay.return_value.returncode = 1
            replay.return_value.stdout = "failed"
            replay.return_value.stderr = ""
            assert "GREEN failed" in record_phase_repro(task, phase="testing")
        replay.assert_called_once()
