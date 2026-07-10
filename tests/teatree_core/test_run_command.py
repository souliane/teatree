import tempfile
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.test import TestCase, override_settings

import teatree.core.management.commands._e2e_discovery as e2e_disc_mod
import teatree.core.management.commands.run as run_mod
import teatree.core.overlay_loader as overlay_loader_mod
import teatree.utils.run as utils_run_mod
from teatree.core.models import Ticket, Worktree
from tests.teatree_core.conftest import CommandOverlay

COMMAND_SETTINGS: dict[str, object] = {}

_MOCK_OVERLAY = {"test": CommandOverlay()}


def _popen_capturing(commands: list[tuple[object, dict[str, object]]], *, returncode: int = 0) -> MagicMock:
    """A ``Popen`` mock that records ``(cmd, kwargs)`` and tees empty stderr.

    Mirrors ``run_streamed``'s context-manager usage (iterate ``proc.stderr``
    then ``proc.wait()``) while letting the test inspect every invocation's
    command list and env, as the old ``subprocess.run`` ``side_effect`` did.
    """

    def factory(*args: object, **kwargs: object) -> MagicMock:
        commands.append((args[0], kwargs))
        proc = MagicMock()
        proc.stderr = iter(())
        proc.wait.return_value = returncode
        ctx = MagicMock()
        ctx.__enter__.return_value = proc
        ctx.__exit__.return_value = False
        return ctx

    return MagicMock(side_effect=factory)


class TestRunCommand(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_verify_transitions_to_ready_and_returns_urls(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            wt_path = str(wt_dir)
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/20", variant="acme")
            wt = Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
            )

            # Manually advance FSM to SERVICES_UP (required for verify)
            wt.provision()
            wt.save()
            wt.start_services(services=["backend", "frontend"])
            wt.save()

            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)

            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(
                    run_mod,
                    "get_worktree_ports",
                    return_value={"backend": 8001, "frontend": 4201},
                ),
                patch.object(
                    run_mod.urllib.request,
                    "urlopen",
                    return_value=mock_resp,
                ),
            ):
                result = cast("dict[str, object]", call_command("run", "verify", path=wt_path))

            worktree = Worktree.objects.get(pk=wt.pk)
            assert result["state"] == Worktree.State.READY
            assert isinstance(result["urls"], dict)
            assert worktree.state == Worktree.State.READY

    @override_settings(**COMMAND_SETTINGS)
    def test_verify_does_not_transition_when_endpoint_fails(self) -> None:
        """When HTTP check raises an exception, verify logs the error and does NOT advance FSM."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            wt_path = str(wt_dir)
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/30", variant="acme")
            wt = Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
            )

            # Manually advance FSM to SERVICES_UP
            wt.provision()
            wt.save()
            wt.start_services(services=["backend", "frontend"])
            wt.save()

            def _fail_urlopen(*_args: object, **_kwargs: object) -> None:
                msg = "Connection refused"
                raise OSError(msg)

            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(
                    run_mod,
                    "get_worktree_ports",
                    return_value={"backend": 8001, "frontend": 4201},
                ),
                patch.object(
                    run_mod.urllib.request,
                    "urlopen",
                    side_effect=_fail_urlopen,
                ),
            ):
                result = cast("dict[str, object]", call_command("run", "verify", path=wt_path))

            worktree = Worktree.objects.get(pk=wt.pk)
            # State should remain SERVICES_UP — not advanced to READY
            assert worktree.state == Worktree.State.SERVICES_UP
            assert result["state"] == Worktree.State.SERVICES_UP
            # Check results contain failure info
            checks = cast("dict[str, dict[str, object]]", result["checks"])
            for check in checks.values():
                assert check["ok"] is False
                assert check["status"] == 0
                assert "Connection refused" in str(check["error"])

    @override_settings(**COMMAND_SETTINGS)
    def test_services_returns_run_commands_from_overlay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            wt_path = str(wt_dir)
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/21", variant="acme")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
            )
            with patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY):
                cast("int", call_command("worktree", "provision", path=wt_path))

                result = cast("dict[str, str]", call_command("run", "services", path=wt_path))

            assert result == {
                "backend": ["run-backend", "/tmp/backend"],
                "frontend": ["run-frontend", "/tmp/backend"],
            }

    @override_settings(**COMMAND_SETTINGS)
    def test_backend_starts_via_docker_compose(self) -> None:
        """Run backend should call docker compose up -d web."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            wt_path = str(wt_dir)
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/50", variant="acme")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": wt_path},
                state=Worktree.State.PROVISIONED,
                db_name="wt_50_acme",
            )

            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path
            mock_overlay = MagicMock()
            mock_overlay.provisioning.compose_file.return_value = "/fake/docker-compose.yml"
            mock_overlay.provisioning.env_extra.return_value = {"DJANGO_SETTINGS_MODULE": "project.settings"}

            commands: list[tuple[object, dict[str, object]]] = []

            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(run_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "Popen", _popen_capturing(commands)),
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                result = cast("str", call_command("run", "backend", path=wt_path))

            assert result == "Backend started via docker-compose."
            # Should have called docker compose up -d web
            assert any("docker" in str(c[0]) and "web" in str(c[0]) for c in commands)


class TestE2eExternalCommand(TestCase):
    @override_settings(**COMMAND_SETTINGS)
    def test_reads_port_from_docker_compose_and_variant_from_env(self) -> None:
        """e2e external reads frontend port from docker compose and variant from the env cache."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            private_tests_dir = tmp_path / "private-tests"
            private_tests_dir.mkdir()

            worktree_dir = tmp_path / "workspace" / "backend"
            worktree_dir.mkdir(parents=True)
            # The env cache lives out-of-repo in the ticket dir's .t3-cache/
            # sibling, never inside the repo working tree (souliane/teatree#3097).
            cache_dir = worktree_dir.parent / ".t3-cache"
            cache_dir.mkdir(parents=True, exist_ok=True)
            envfile = cache_dir / ".t3-env.cache"
            envfile.write_text("WT_VARIANT=acme\n", encoding="utf-8")

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/80", variant="acme")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(worktree_dir)},
                state=Worktree.State.SERVICES_UP,
                db_name="wt_80_acme",
            )

            commands: list[tuple[object, dict[str, object]]] = []

            with (
                patch.dict(
                    "os.environ",
                    {
                        "T3_PRIVATE_TESTS": str(private_tests_dir),
                        "T3_ORIG_CWD": str(worktree_dir),
                    },
                ),
                patch.object(overlay_loader_mod, "_discover_overlays", return_value=_MOCK_OVERLAY),
                patch.object(e2e_disc_mod, "get_service_port", return_value=4299),
                patch.object(utils_run_mod, "Popen", _popen_capturing(commands)),
            ):
                result = cast("str", call_command("e2e", "external"))

            captured_envs = [cast("dict[str, str]", kwargs["env"]) for _cmd, kwargs in commands if "env" in kwargs]
            assert result == "E2E passed."
            assert captured_envs
            assert captured_envs[-1]["BASE_URL"] == "http://localhost:4299"
            assert captured_envs[-1]["CUSTOMER"] == "acme"

    @override_settings(**COMMAND_SETTINGS)
    def test_raises_system_exit_when_frontend_not_running(self) -> None:
        """e2e external must exit 1 when the frontend service is not running (#932)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            private_tests_dir = tmp_path / "private-tests"
            private_tests_dir.mkdir()

            worktree_dir = tmp_path / "workspace" / "backend"
            worktree_dir.mkdir(parents=True)

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/81", variant="acme")
            Worktree.objects.create(
                ticket=ticket,
                overlay="test",
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(worktree_dir)},
                state=Worktree.State.SERVICES_UP,
                db_name="wt_81_acme",
            )

            with (
                patch.dict(
                    "os.environ",
                    {
                        "T3_PRIVATE_TESTS": str(private_tests_dir),
                        "T3_ORIG_CWD": str(worktree_dir),
                    },
                ),
                patch.object(e2e_disc_mod, "get_service_port", return_value=None),
                patch.object(e2e_disc_mod, "detect_local_port", return_value=None),
                pytest.raises(SystemExit) as exc_info,
            ):
                call_command("e2e", "external")

            assert exc_info.value.code == 1


class TestCliOverlay:
    def test_managepy_calls_uv(self, tmp_path: Path) -> None:
        from teatree.cli.overlay import managepy  # noqa: PLC0415

        commands: list[tuple[object, dict[str, object]]] = []
        (tmp_path / "manage.py").write_text("# stub", encoding="utf-8")
        with patch.object(utils_run_mod, "Popen", _popen_capturing(commands)):
            managepy(tmp_path, "migrate", "--no-input")

        assert len(commands) == 1
        cmd = cast("list[str]", commands[0][0])
        assert Path(cmd[0]).name == "uv"
        assert cmd[1:3] == ["--directory", str(tmp_path)]
        assert cmd[-2:] == ["migrate", "--no-input"]
