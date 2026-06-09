"""Tests for the run management command."""

import os
import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands._e2e_discovery as e2e_disc_mod
import teatree.core.management.commands.e2e as e2e_mod
import teatree.core.management.commands.run as run_mod
import teatree.utils.run as utils_run_mod
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.management_commands._overlays import FULL_OVERLAY, MINIMAL_OVERLAY, SETTINGS, _patch_overlays

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


def _popen_mock(returncode: int = 0, stderr: str = "") -> MagicMock:
    """Build a ``Popen`` context-manager mock matching ``run_streamed``'s usage.

    ``run_streamed`` tees stderr (iterates ``proc.stderr``) then calls
    ``proc.wait()``; the mock exposes both so a test can assert the streamed
    command was invoked and drive the surfaced exit code.
    """
    proc = MagicMock()
    proc.stderr = iter(stderr.splitlines(keepends=True))
    proc.wait.return_value = returncode
    ctx = MagicMock()
    ctx.__enter__.return_value = proc
    ctx.__exit__.return_value = False
    return MagicMock(return_value=ctx)


# ── Run commands ───────────────────────────────────────────────────


class TestRunBackend(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_starts_via_docker_compose(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path
            mock_run = _popen_mock()
            with (
                patch.object(utils_run_mod, "Popen", mock_run),
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                result = cast("str", call_command("run", "backend", path=str(wt_dir)))

            mock_run.assert_called_once()
            assert "docker-compose" in result.lower()

    @_patch_overlays(MINIMAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_compose_file_returns_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path
            with (
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                result = cast("str", call_command("run", "backend", path=str(wt_dir)))

            assert "no docker-compose file" in result.lower()


class TestRunBuildFrontend(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_calls_overlay_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "frontend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/frontend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            mock_run = _popen_mock()
            with patch.object(utils_run_mod, "Popen", mock_run):
                result = cast("str", call_command("run", "build-frontend", path=str(wt_dir)))

            mock_run.assert_called_once()
            # build-frontend now routes through ServiceLauncher (pre-run steps
            # then the command); the launcher reports the service + exit code.
            assert "build-frontend" in result.lower()
            assert "rc=0" in result

    @_patch_overlays(MINIMAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_command_returns_message(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "frontend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/frontend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            result = cast("str", call_command("run", "build-frontend", path=str(wt_dir)))

            assert "no run command configured" in result.lower()
            assert "build-frontend" in result.lower()


class TestRunTests(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_calls_overlay_test_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            mock_run = _popen_mock()
            with patch.object(utils_run_mod, "Popen", mock_run):
                result = cast("str", call_command("run", "tests", path=str(wt_dir)))

            mock_run.assert_called_once()
            assert "completed" in result.lower()

    @_patch_overlays(MINIMAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_command_raises_system_exit(self) -> None:
        """`run tests` with no configured test command is a genuine failure.

        The caller explicitly asked to run the suite; an overlay that cannot
        run tests must stop the caller (CI/loop), not exit 0.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            with pytest.raises(SystemExit) as exc_info:
                call_command("run", "tests", path=str(wt_dir))

            assert exc_info.value.code == 1


class TestRunTestsFailureExitCode(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_failing_suite_raises_system_exit_1(self) -> None:
        """A non-zero test runner exit must surface as SystemExit(1).

        Regression for #932: `return f"Tests failed (exit {rc})."` left the
        process exiting 0, so CI/loop saw green on a failing suite.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            with (
                patch.object(utils_run_mod, "Popen", _popen_mock(returncode=1)),
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("run", "tests", path=str(wt_dir))

            assert exc_info.value.code == 1


class TestRunLint(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_calls_overlay_lint_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            mock_run = _popen_mock()
            with patch.object(utils_run_mod, "Popen", mock_run):
                result = cast("str", call_command("run", "lint", path=str(wt_dir)))

            mock_run.assert_called_once()
            assert "completed" in result.lower()

    @_patch_overlays(MINIMAL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_no_command_raises_system_exit(self) -> None:
        """`run lint` with no configured lint command is a genuine failure.

        The caller explicitly asked to lint; an overlay that cannot lint must
        stop the caller (CI/loop), not exit 0.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            with pytest.raises(SystemExit) as exc_info:
                call_command("run", "lint", path=str(wt_dir))

            assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_failing_lint_raises_system_exit_1(self) -> None:
        """A non-zero lint exit must surface as SystemExit(1) so CI/loop sees red."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            with (
                patch.object(utils_run_mod, "Popen", _popen_mock(returncode=1)),
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("run", "lint", path=str(wt_dir))

            assert exc_info.value.code == 1


class TestRunVerify(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_verifies_endpoints_and_advances_fsm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/110")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature-110",
                extra={"worktree_path": str(wt_dir)},
            )
            wt.provision()
            wt.start_services(services=["backend"])
            wt.save()

            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)

            with (
                patch.object(run_mod, "get_worktree_ports", return_value={"backend": 8001}),
                patch.object(run_mod, "resolve_worktree", return_value=wt),
                patch.object(run_mod.urllib.request, "urlopen", return_value=mock_response),
            ):
                result = cast("dict[str, object]", call_command("run", "verify", path=str(wt_dir)))

            wt.refresh_from_db()
            assert wt.state == Worktree.State.READY
            assert result["state"] == Worktree.State.READY

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_merges_env_health_endpoints(self) -> None:
        """T3_HEALTH_ENDPOINTS env var overrides overlay-provided endpoints."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/111")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature-111",
                extra={"worktree_path": str(wt_dir)},
            )
            wt.provision()
            wt.start_services(services=["backend"])
            wt.save()

            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)

            urls_checked: list[str] = []

            def capture_urlopen(url, **kwargs):
                urls_checked.append(url)
                return mock_response

            with (
                patch.object(run_mod, "get_worktree_ports", return_value={"backend": 8001}),
                patch.object(run_mod, "resolve_worktree", return_value=wt),
                patch.object(run_mod.urllib.request, "urlopen", side_effect=capture_urlopen),
                patch.dict(os.environ, {"T3_HEALTH_ENDPOINTS": "backend:/api/health"}),
            ):
                call_command("run", "verify", path=str(wt_dir))

            # Should use the env var path, not the overlay default
            assert any("/api/health" in url for url in urls_checked)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_skips_postgres_and_redis(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/112")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature-112",
                extra={"worktree_path": str(wt_dir)},
            )
            wt.provision()
            wt.start_services(services=["backend"])
            wt.save()

            urls_checked: list[str] = []

            mock_response = MagicMock()
            mock_response.status = 200
            mock_response.__enter__ = MagicMock(return_value=mock_response)
            mock_response.__exit__ = MagicMock(return_value=False)

            def capture_urlopen(url, **kwargs):
                urls_checked.append(url)
                return mock_response

            with (
                patch.object(
                    run_mod,
                    "get_worktree_ports",
                    return_value={"backend": 8001, "postgres": 5432, "redis": 6379},
                ),
                patch.object(run_mod, "resolve_worktree", return_value=wt),
                patch.object(run_mod.urllib.request, "urlopen", side_effect=capture_urlopen),
            ):
                call_command("run", "verify", path=str(wt_dir))

            # Only backend should be checked, not postgres/redis
            assert len(urls_checked) == 1
            assert "8001" in urls_checked[0]


class TestRunServices(TestCase):
    pass  # No standalone services tests in the original file — placeholder for future tests


class TestPlaywrightOptions:
    def test_update_snapshots_flag(self) -> None:
        opts = e2e_mod.PlaywrightOptions(update_snapshots=True)
        args = opts.to_args()
        assert "--update-snapshots" in args
        assert "--reporter=list" in args

    def test_no_update_snapshots(self) -> None:
        opts = e2e_mod.PlaywrightOptions()
        args = opts.to_args()
        assert "--update-snapshots" not in args


class TestDetectLocalPort:
    def test_returns_port_when_listening(self) -> None:
        with patch("teatree.core.management.commands._e2e_discovery.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_sock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_sock.connect_ex.return_value = 0
            assert e2e_disc_mod.detect_local_port(8080) == 8080

    def test_returns_none_when_not_listening(self) -> None:
        with patch("teatree.core.management.commands._e2e_discovery.socket.socket") as mock_sock_cls:
            mock_sock = MagicMock()
            mock_sock_cls.return_value.__enter__ = MagicMock(return_value=mock_sock)
            mock_sock_cls.return_value.__exit__ = MagicMock(return_value=False)
            mock_sock.connect_ex.return_value = 1
            assert e2e_disc_mod.detect_local_port(8080) is None


class TestDiscoverFrontendPort:
    def test_returns_docker_port_when_compose_reports_it(self) -> None:
        with (
            patch.object(e2e_disc_mod, "ticket_frontend_projects", return_value=["project"]),
            patch.object(e2e_disc_mod, "get_service_port", return_value=4201),
        ):
            assert e2e_disc_mod.discover_frontend_port(MagicMock()) == 4201

    def test_scans_local_ports_as_final_fallback(self) -> None:
        with (
            patch.object(e2e_disc_mod, "ticket_frontend_projects", return_value=["project"]),
            patch.object(e2e_disc_mod, "get_service_port", return_value=None),
            patch.object(e2e_disc_mod, "detect_local_port", side_effect=lambda p: 4203 if p == 4203 else None),
        ):
            assert e2e_disc_mod.discover_frontend_port(MagicMock()) == 4203

    def test_returns_none_when_no_port_found(self) -> None:
        with (
            patch.object(e2e_disc_mod, "ticket_frontend_projects", return_value=["project"]),
            patch.object(e2e_disc_mod, "get_service_port", return_value=None),
            patch.object(e2e_disc_mod, "detect_local_port", return_value=None),
        ):
            assert e2e_disc_mod.discover_frontend_port(MagicMock()) is None


class TestRunHealthChecks(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_failed_health_check_reports_failure(self) -> None:
        pytest.skip(
            "_run_health_checks command helper removed in worktree FSM refactor — "
            "health checks now run inside WorktreeVerifyRunner; needs rewrite as "
            "integration test against call_command('worktree', 'verify', ...)",
        )

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_health_check_exception_reports_error(self) -> None:
        pytest.skip(
            "_run_health_checks command helper removed in worktree FSM refactor — "
            "health checks now run inside WorktreeVerifyRunner; needs rewrite as "
            "integration test against call_command('worktree', 'verify', ...)",
        )
