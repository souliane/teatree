"""``worktree start``/``verify`` and ``workspace start`` chain into readiness probes.

Issue #479 MR2: starting (or verifying) a worktree without checking readiness
probes lets a silently broken environment look healthy. The lifecycle commands
must exit 1 when any probe fails — same gate as ``ready``.
"""

import io
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands.workspace as workspace_mod
import teatree.core.management.commands.worktree as worktree_mod
from teatree.core.models import Ticket, Worktree
from teatree.core.readiness import Probe, ProbeResult
from teatree.core.runners.base import RunnerResult

SETTINGS = {
    "TEATREE_OVERLAY_NAMES": ["test"],
}


def _passing_probe(name: str = "p1") -> Probe:
    return Probe(
        name=name,
        description="d",
        check_fn=lambda: ProbeResult(name=name, passed=True, reason="ok"),
    )


def _failing_probe(name: str = "p1", reason: str = "nope") -> Probe:
    return Probe(
        name=name,
        description="d",
        check_fn=lambda: ProbeResult(name=name, passed=False, reason=reason),
    )


def _build_worktree(wt_path: Path, *, repo_path: str = "backend") -> Worktree:
    ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/479")
    return Worktree.objects.create(
        overlay="test",
        ticket=ticket,
        repo_path=repo_path,
        branch="feature",
        db_name="test_db",
        extra={"worktree_path": str(wt_path)},
        state=Worktree.State.PROVISIONED,
    )


@override_settings(**SETTINGS)
class TestWorktreeStartChainsProbes(TestCase):
    def test_start_exits_1_when_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            wt = _build_worktree(wt_path)

            mock_overlay = MagicMock()
            mock_overlay.get_run_commands.return_value = {}
            mock_overlay.get_readiness_probes.return_value = [
                _failing_probe("translations-loaded", reason="raw key visible"),
            ]

            mock_runner = MagicMock()
            mock_runner.run.return_value = RunnerResult(ok=True, detail="started")

            mock_config = MagicMock()
            mock_config.user.workspace_dir = Path(tmp)

            with (
                patch.object(worktree_mod, "resolve_worktree", return_value=wt),
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(worktree_mod, "WorktreeStartRunner", return_value=mock_runner),
                patch.object(worktree_mod, "find_free_ports", return_value={"backend": 8001}),
                patch("teatree.config.load_config", return_value=mock_config),
                pytest.raises(SystemExit) as exc,
            ):
                call_command("worktree", "start", path=str(wt_path))
            assert exc.value.code == 1

    def test_start_returns_state_when_all_probes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            wt = _build_worktree(wt_path)

            mock_overlay = MagicMock()
            mock_overlay.get_run_commands.return_value = {}
            mock_overlay.get_readiness_probes.return_value = [_passing_probe("backend-up")]

            mock_runner = MagicMock()
            mock_runner.run.return_value = RunnerResult(ok=True, detail="started")

            mock_config = MagicMock()
            mock_config.user.workspace_dir = Path(tmp)

            with (
                patch.object(worktree_mod, "resolve_worktree", return_value=wt),
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(worktree_mod, "WorktreeStartRunner", return_value=mock_runner),
                patch.object(worktree_mod, "find_free_ports", return_value={"backend": 8001}),
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                result = call_command("worktree", "start", path=str(wt_path))
            assert result == Worktree.State.SERVICES_UP

    def test_start_returns_state_when_no_probes_defined(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            wt = _build_worktree(wt_path)

            mock_overlay = MagicMock()
            mock_overlay.get_run_commands.return_value = {}
            mock_overlay.get_readiness_probes.return_value = []

            mock_runner = MagicMock()
            mock_runner.run.return_value = RunnerResult(ok=True, detail="started")

            mock_config = MagicMock()
            mock_config.user.workspace_dir = Path(tmp)

            with (
                patch.object(worktree_mod, "resolve_worktree", return_value=wt),
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(worktree_mod, "WorktreeStartRunner", return_value=mock_runner),
                patch.object(worktree_mod, "find_free_ports", return_value={"backend": 8001}),
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                result = call_command("worktree", "start", path=str(wt_path))
            assert result == Worktree.State.SERVICES_UP


@override_settings(**SETTINGS)
class TestWorktreeVerifyChainsProbes(TestCase):
    def test_verify_exits_1_when_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            wt = _build_worktree(wt_path)
            wt.state = Worktree.State.SERVICES_UP
            wt.save(update_fields=["state"])

            mock_overlay = MagicMock()
            mock_overlay.get_readiness_probes.return_value = [
                _failing_probe("cors", reason="missing Access-Control-Allow-Origin"),
            ]

            mock_runner = MagicMock()
            mock_runner.run.return_value = RunnerResult(ok=True, detail="verified")

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(worktree_mod, "resolve_worktree", return_value=wt),
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(worktree_mod, "WorktreeVerifyRunner", return_value=mock_runner),
                pytest.raises(SystemExit) as exc,
            ):
                call_command("worktree", "verify", path=str(wt_path), stdout=stdout, stderr=stderr)
            assert exc.value.code == 1
            assert "cors" in stdout.getvalue() + stderr.getvalue()

    def test_verify_returns_state_when_all_probes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            wt = _build_worktree(wt_path)
            wt.state = Worktree.State.SERVICES_UP
            wt.save(update_fields=["state"])

            mock_overlay = MagicMock()
            mock_overlay.get_readiness_probes.return_value = [_passing_probe("api-up")]

            mock_runner = MagicMock()
            mock_runner.run.return_value = RunnerResult(ok=True, detail="verified")

            with (
                patch.object(worktree_mod, "resolve_worktree", return_value=wt),
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(worktree_mod, "WorktreeVerifyRunner", return_value=mock_runner),
            ):
                result = call_command("worktree", "verify", path=str(wt_path))
            assert result == Worktree.State.READY


@override_settings(**SETTINGS)
class TestWorkspaceStartChainsProbes(TestCase):
    def test_start_exits_1_when_any_worktree_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_a = tmp_path / "a"
            wt_a.mkdir()
            wt_b = tmp_path / "b"
            wt_b.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/479")
            anchor = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="db_a",
                extra={"worktree_path": str(wt_a)},
                state=Worktree.State.PROVISIONED,
            )
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="frontend",
                branch="feature",
                db_name="db_b",
                extra={"worktree_path": str(wt_b)},
                state=Worktree.State.PROVISIONED,
            )

            def probes_for(wt: Worktree) -> list[Probe]:
                if wt.repo_path == "frontend":
                    return [_failing_probe("frontend-up", "503")]
                return [_passing_probe("backend-up")]

            mock_overlay = MagicMock()
            mock_overlay.get_run_commands.return_value = {}
            mock_overlay.get_readiness_probes.side_effect = probes_for

            mock_runner = MagicMock()
            mock_runner.run.return_value = RunnerResult(ok=True, detail="started")

            with (
                patch.object(workspace_mod, "resolve_worktree", return_value=anchor),
                patch.object(workspace_mod, "get_overlay", return_value=mock_overlay),
                patch.object(workspace_mod, "WorktreeStartRunner", return_value=mock_runner),
                patch.object(workspace_mod, "find_free_ports", return_value={"backend": 8001}),
                pytest.raises(SystemExit) as exc,
            ):
                call_command("workspace", "start", path=str(wt_a))
            assert exc.value.code == 1

    def test_start_returns_summary_when_all_probes_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_a = tmp_path / "a"
            wt_a.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/479")
            anchor = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="db_a",
                extra={"worktree_path": str(wt_a)},
                state=Worktree.State.PROVISIONED,
            )

            mock_overlay = MagicMock()
            mock_overlay.get_run_commands.return_value = {}
            mock_overlay.get_readiness_probes.return_value = [_passing_probe("backend-up")]

            mock_runner = MagicMock()
            mock_runner.run.return_value = RunnerResult(ok=True, detail="started")

            with (
                patch.object(workspace_mod, "resolve_worktree", return_value=anchor),
                patch.object(workspace_mod, "get_overlay", return_value=mock_overlay),
                patch.object(workspace_mod, "WorktreeStartRunner", return_value=mock_runner),
                patch.object(workspace_mod, "find_free_ports", return_value={"backend": 8001}),
            ):
                result = call_command("workspace", "start", path=str(wt_a))
            assert "started" in result

    def test_start_returns_summary_when_no_probes_anywhere(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_a = tmp_path / "a"
            wt_a.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/479")
            anchor = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="db_a",
                extra={"worktree_path": str(wt_a)},
                state=Worktree.State.PROVISIONED,
            )

            mock_overlay = MagicMock()
            mock_overlay.get_run_commands.return_value = {}
            mock_overlay.get_readiness_probes.return_value = []

            mock_runner = MagicMock()
            mock_runner.run.return_value = RunnerResult(ok=True, detail="started")

            with (
                patch.object(workspace_mod, "resolve_worktree", return_value=anchor),
                patch.object(workspace_mod, "get_overlay", return_value=mock_overlay),
                patch.object(workspace_mod, "WorktreeStartRunner", return_value=mock_runner),
                patch.object(workspace_mod, "find_free_ports", return_value={"backend": 8001}),
            ):
                result = call_command("workspace", "start", path=str(wt_a))
            assert "started" in result
