"""Tests for ``worktree ready`` and ``workspace ready`` CLI commands."""

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
        state=Worktree.State.SERVICES_UP,
    )


@override_settings(**SETTINGS)
class TestWorktreeReady(TestCase):
    def test_no_probes_returns_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            wt = _build_worktree(wt_path)

            mock_overlay = MagicMock()
            mock_overlay.get_readiness_probes.return_value = []

            with (
                patch.object(worktree_mod, "resolve_worktree", return_value=wt),
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
            ):
                result = call_command("worktree", "ready", path=str(wt_path))
            assert result == "ok"

    def test_all_probes_pass_returns_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            wt = _build_worktree(wt_path)

            mock_overlay = MagicMock()
            mock_overlay.get_readiness_probes.return_value = [
                _passing_probe("backend"),
                _passing_probe("frontend"),
            ]

            with (
                patch.object(worktree_mod, "resolve_worktree", return_value=wt),
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
            ):
                result = call_command("worktree", "ready", path=str(wt_path))
            assert result == "ok"

    def test_any_probe_failure_raises_systemexit_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            wt = _build_worktree(wt_path)

            mock_overlay = MagicMock()
            mock_overlay.get_readiness_probes.return_value = [
                _passing_probe("backend"),
                _failing_probe("frontend", reason="connection refused"),
            ]

            with (
                patch.object(worktree_mod, "resolve_worktree", return_value=wt),
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                pytest.raises(SystemExit) as exc,
            ):
                call_command("worktree", "ready", path=str(wt_path))
            assert exc.value.code == 1

    def test_failure_output_names_failed_probes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "worktree"
            wt_path.mkdir()
            wt = _build_worktree(wt_path)

            mock_overlay = MagicMock()
            mock_overlay.get_readiness_probes.return_value = [
                _failing_probe("translations-loaded", reason="raw key visible"),
            ]

            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch.object(worktree_mod, "resolve_worktree", return_value=wt),
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                pytest.raises(SystemExit),
            ):
                call_command("worktree", "ready", path=str(wt_path), stdout=stdout, stderr=stderr)
            combined = stdout.getvalue() + stderr.getvalue()
            assert "translations-loaded" in combined
            assert "raw key visible" in combined
            assert "1 of 1" in combined  # failure count line


@override_settings(**SETTINGS)
class TestWorkspaceReady(TestCase):
    def test_loops_over_all_worktrees_in_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_a = tmp_path / "a"
            wt_a.mkdir()
            wt_b = tmp_path / "b"
            wt_b.mkdir()

            ticket = Ticket.objects.create(
                overlay="test",
                issue_url="https://example.com/issues/479",
            )
            anchor = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="db_a",
                extra={"worktree_path": str(wt_a)},
                state=Worktree.State.SERVICES_UP,
            )
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="frontend",
                branch="feature",
                db_name="db_b",
                extra={"worktree_path": str(wt_b)},
                state=Worktree.State.SERVICES_UP,
            )

            mock_overlay = MagicMock()
            mock_overlay.get_readiness_probes.side_effect = lambda wt: [_passing_probe(f"{wt.repo_path}-up")]

            with (
                patch.object(workspace_mod, "resolve_worktree", return_value=anchor),
                patch.object(workspace_mod, "get_overlay", return_value=mock_overlay),
            ):
                result = call_command("workspace", "ready", path=str(wt_a))
            assert result == "ok"
            assert mock_overlay.get_readiness_probes.call_count == 2

    def test_any_worktree_failure_exits_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_a = tmp_path / "a"
            wt_a.mkdir()
            wt_b = tmp_path / "b"
            wt_b.mkdir()

            ticket = Ticket.objects.create(
                overlay="test",
                issue_url="https://example.com/issues/479",
            )
            anchor = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="db_a",
                extra={"worktree_path": str(wt_a)},
                state=Worktree.State.SERVICES_UP,
            )
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="frontend",
                branch="feature",
                db_name="db_b",
                extra={"worktree_path": str(wt_b)},
                state=Worktree.State.SERVICES_UP,
            )

            def probes_for(wt: Worktree) -> list[Probe]:
                if wt.repo_path == "frontend":
                    return [_failing_probe("frontend-up", "503")]
                return [_passing_probe("backend-up")]

            mock_overlay = MagicMock()
            mock_overlay.get_readiness_probes.side_effect = probes_for

            with (
                patch.object(workspace_mod, "resolve_worktree", return_value=anchor),
                patch.object(workspace_mod, "get_overlay", return_value=mock_overlay),
                pytest.raises(SystemExit) as exc,
            ):
                call_command("workspace", "ready", path=str(wt_a))
            assert exc.value.code == 1

    def test_no_probes_anywhere_returns_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            wt_path = Path(tmp) / "wt"
            wt_path.mkdir()
            ticket = Ticket.objects.create(
                overlay="test",
                issue_url="https://example.com/issues/479",
            )
            anchor = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                db_name="db",
                extra={"worktree_path": str(wt_path)},
                state=Worktree.State.SERVICES_UP,
            )

            mock_overlay = MagicMock()
            mock_overlay.get_readiness_probes.return_value = []

            with (
                patch.object(workspace_mod, "resolve_worktree", return_value=anchor),
                patch.object(workspace_mod, "get_overlay", return_value=mock_overlay),
            ):
                result = call_command("workspace", "ready", path=str(wt_path))
            assert result == "ok"
