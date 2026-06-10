"""Tests for the lifecycle management command."""

import subprocess
import tempfile
from io import StringIO
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from django.core.management import call_command
from django.core.management.base import OutputWrapper
from django.test import TestCase, override_settings
from django.utils.module_loading import import_string

import teatree.core.management.commands.worktree as worktree_mod
import teatree.core.overlay_loader as overlay_loader_mod
import teatree.core.skill_cache as startup_mod
import teatree.utils.db as db_mod
import teatree.utils.run as utils_run_mod
from teatree.core.models import Session, Ticket, Worktree
from teatree.core.overlay import ProvisionStep
from tests.teatree_core.management_commands._overlays import (
    FAILING_IMPORT_OVERLAY,
    FULL_OVERLAY,
    POST_DB_OVERLAY,
    PRE_RUN_OVERLAY,
    SETTINGS,
    _patch_overlays,
    env_safe_mock_overlay,
)

pytestmark = pytest.mark.filterwarnings(
    "ignore:In Typer, only the parameter 'autocompletion' is supported.*:DeprecationWarning",
)


# ── Lifecycle commands ──────────────────────────────────────────────


class TestLifecycleSetup(TestCase):
    def setUp(self) -> None:
        super().setUp()
        mock_sp = MagicMock()
        mock_sp.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_sp.TimeoutExpired = subprocess.TimeoutExpired
        mock_sp.CompletedProcess = subprocess.CompletedProcess
        self.enterContext(patch.object(utils_run_mod, "subprocess", mock_sp))

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_reset_passwords(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/60")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            reset_called = False

            def _track_reset() -> None:
                nonlocal reset_called
                reset_called = True

            overlay = import_string(FULL_OVERLAY)()
            overlay.get_reset_passwords_command = lambda wt: ProvisionStep(name="reset", callable=_track_reset)

            with (
                patch.object(overlay_loader_mod, "_discover_overlays", return_value={"test": overlay}),
                patch.object(utils_run_mod, "subprocess"),
            ):
                call_command("worktree", "provision", path=str(wt_dir))

            assert reset_called

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_already_provisioned_skips_provision(self) -> None:
        """When worktree is already provisioned, setup skips the provision step."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/61")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            wt.provision()
            wt.save()

            with patch.object(utils_run_mod.subprocess, "run"):
                worktree_id = cast("int", call_command("worktree", "provision", path=str(wt_dir)))

            worktree = Worktree.objects.get(pk=worktree_id)
            assert worktree.state == Worktree.State.PROVISIONED

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_variant_option_updates_ticket(self) -> None:
        """The --variant option updates the ticket variant before provisioning."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/90", variant="")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            with patch.object(utils_run_mod.subprocess, "run"):
                call_command("worktree", "provision", path=str(wt_dir), variant="testcustomer")

            ticket.refresh_from_db()
            assert ticket.variant == "testcustomer"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_variant_propagates_to_db_name_and_env_cache(self) -> None:
        """`--variant` must reach db_name and the rendered WT_VARIANT/WT_DB_NAME.

        The in-scope worktree resolved by the command holds a cached ``ticket``
        FK loaded before the variant update. When ``_build_db_name`` and the env
        render read that stale FK, ``WT_VARIANT`` renders blank and ``db_name``
        loses its variant suffix — so the DB import targets the wrong name. The
        command must refresh the FK so both reflect the new variant.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/91", variant="")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            with patch.object(utils_run_mod.subprocess, "run"):
                worktree_id = cast(
                    "int",
                    call_command("worktree", "provision", path=str(wt_dir), variant="acmebank"),
                )

            worktree = Worktree.objects.get(pk=worktree_id)
            assert worktree.db_name == "wt_91_acmebank"

            cache_file = tmp_path / ".t3-cache" / ".t3-env.cache"
            cache_body = cache_file.read_text(encoding="utf-8")
            assert "WT_VARIANT=acmebank" in cache_body
            assert "WT_DB_NAME=wt_91_acmebank" in cache_body

    @_patch_overlays(FAILING_IMPORT_OVERLAY)
    @override_settings(**SETTINGS)
    def test_aborts_on_db_import_failure(self) -> None:
        """A failed db_import aborts provision with SystemExit(1) (#2208 fail-loud).

        Previously the runner warned and continued, marking the worktree
        PROVISIONED with no test DB. #2208 restores the same posture as the
        standalone ``t3 db import``: a failed import is a provision failure.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/70")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            with patch.object(utils_run_mod, "subprocess") as mock_sp:
                mock_sp.run.return_value = MagicMock(returncode=0)
                with pytest.raises(SystemExit) as exc_info:
                    call_command("worktree", "provision", path=str(wt_dir))

            assert exc_info.value.code == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_post_db_steps(self) -> None:
        """Setup runs post-DB steps from the overlay via the step runner."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/71")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            # FullOverlay.get_reset_passwords_command returns a step with callable=lambda: None.
            # The step runner invokes callables directly (no subprocess).
            # Verify setup completes without error — the step runner handles execution.
            call_command("worktree", "provision", path=str(wt_dir))

    @_patch_overlays(POST_DB_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_post_db_steps_with_commands(self) -> None:
        """Setup iterates post-DB steps and runs their callables via the step runner."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/72")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            # PostDbStepsOverlay returns named callable steps.
            # The step runner invokes each callable and tracks results.
            call_command("worktree", "provision", path=str(wt_dir))

    @_patch_overlays(PRE_RUN_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_pre_run_steps_for_all_services(self) -> None:
        """Setup calls get_pre_run_steps for every service from get_run_commands."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/73")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            call_command("worktree", "provision", path=str(wt_dir))

            # PreRunOverlay.get_run_commands returns backend, frontend, build-frontend
            wt.refresh_from_db()
            assert sorted((wt.extra or {}).get("pre_run_log", [])) == ["backend", "build-frontend", "frontend"]

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_writes_skill_metadata_cache(self) -> None:
        """Setup writes the overlay skill metadata to DATA_DIR/skill-metadata.json."""
        pytest.skip(
            "skill-metadata cache is now written on Django startup, "
            "not during worktree provision — needs rewrite to assert startup behavior",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/63")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )

            with patch.object(startup_mod, "DATA_DIR", tmp_path):
                call_command("worktree", "provision", path=str(wt_dir))

            cache_file = tmp_path / "skill-metadata.json"
            assert cache_file.exists()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_runs_prek_install_when_config_exists(self) -> None:
        """Setup runs 'prek install -f' when .pre-commit-config.yaml exists in worktree path."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_path = tmp_path / "worktree"
            wt_path.mkdir()
            (wt_path / ".pre-commit-config.yaml").write_text("repos: []\n", encoding="utf-8")

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/100")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_path)},
            )

            with patch.object(utils_run_mod, "subprocess") as mock_sp:
                mock_sp.run.return_value = MagicMock(returncode=0, stdout="", stderr="")
                mock_sp.TimeoutExpired = subprocess.TimeoutExpired
                mock_sp.CompletedProcess = subprocess.CompletedProcess
                call_command("worktree", "provision", path=str(wt_path))

            # Find the prek install call among all subprocess.run calls
            prek_calls = [
                c for c in mock_sp.run.call_args_list if c[0] and isinstance(c[0][0], list) and "prek" in c[0][0]
            ]
            assert len(prek_calls) == 1
            assert prek_calls[0][0][0] == ["prek", "install", "-f"]
            assert prek_calls[0][1].get("cwd") == str(wt_path)

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_appends_envrc_lines_from_overlay(self) -> None:
        """Setup appends overlay .envrc lines (e.g. venv activation) to worktree .envrc."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_path = tmp_path / "worktree"
            wt_path.mkdir()
            (wt_path / ".envrc").write_text("# existing\n", encoding="utf-8")

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/200")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_path)},
            )

            mock_overlay = env_safe_mock_overlay()
            mock_overlay.get_envrc_lines.return_value = ["export USE_UV=1"]
            mock_overlay.get_provision_steps.return_value = []
            mock_overlay.get_post_db_steps.return_value = []
            mock_overlay.get_reset_passwords_command.return_value = ""
            mock_overlay.metadata.get_skill_metadata.return_value = {}

            with (
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
            ):
                mock_sp.run.return_value = MagicMock(returncode=0)
                call_command("worktree", "provision", path=str(wt_path))

            envrc = (wt_path / ".envrc").read_text()
            assert "export USE_UV=1" in envrc
            assert "# existing" in envrc  # original content preserved

            # Run again — should not duplicate
            with (
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
            ):
                mock_sp.run.return_value = MagicMock(returncode=0)
                call_command("worktree", "provision", path=str(wt_path))

            envrc2 = (wt_path / ".envrc").read_text()
            assert envrc2.count("export USE_UV=1") == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_updates_ticket_variant_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path = tmp_path / "worktree"
            wt_path.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/201", variant="alpha")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_path)},
            )

            mock_overlay = env_safe_mock_overlay()
            mock_overlay.get_envrc_lines.return_value = []
            mock_overlay.get_provision_steps.return_value = []
            mock_overlay.get_post_db_steps.return_value = []
            mock_overlay.get_reset_passwords_command.return_value = ""
            mock_overlay.metadata.get_skill_metadata.return_value = {}

            with (
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
            ):
                mock_sp.run.return_value = MagicMock(returncode=0)
                call_command("worktree", "provision", path=str(wt_path), variant="beta")

            ticket.refresh_from_db()
            assert ticket.variant == "beta"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_skips_envfile_message_when_no_path(self) -> None:
        """Setup skips 'Written:' message when write_env_cache returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_path = tmp_path / "worktree"
            wt_path.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/251")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_path)},
            )

            mock_overlay = env_safe_mock_overlay()
            mock_overlay.get_provision_steps.return_value = []
            mock_overlay.get_post_db_steps.return_value = []
            mock_overlay.get_reset_passwords_command.return_value = ""
            mock_overlay.get_envrc_lines.return_value = []
            mock_overlay.metadata.get_skill_metadata.return_value = {}

            with (
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch("teatree.core.runners.worktree_provision.write_env_cache", return_value=None),
            ):
                mock_sp.run.return_value = MagicMock(returncode=0)
                call_command("worktree", "provision", path=str(wt_path))

    @override_settings(**SETTINGS)
    def test_prints_diagnostic_summary(self) -> None:
        """_print_diagnostics outputs a structured checklist with [OK]/[FAIL] markers."""
        pytest.skip(
            "_print_diagnostics removed in worktree FSM refactor — "
            "diagnostics moved to the t3 worktree diagnose subcommand; needs rewrite",
        )
        from teatree.core.step_runner import ProvisionReport, StepResult  # noqa: PLC0415

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            # Create env cache in parent (ticket dir)
            cache_dir = tmp_path / ".t3-cache"
            cache_dir.mkdir()
            (cache_dir / ".t3-env.cache").write_text("WT_DB_NAME=test\n")

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/115")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature-115",
                extra={"worktree_path": str(wt_dir)},
                db_name="wt_115",
            )

            report = ProvisionReport(
                steps=[
                    StepResult(name="migrations", success=True, duration=1.0),
                    StepResult(name="docker-up", success=False, duration=0.5, error="exit 1"),
                ],
            )

            buf = StringIO()
            cmd = worktree_mod.Command()
            cmd.stdout = OutputWrapper(buf)
            cmd._print_diagnostics(wt, report)

            output = buf.getvalue()
            assert "[OK] worktree dir" in output
            assert "[OK] .t3-env.cache" in output
            assert "[OK] DB name" in output
            assert "[OK] migrations" in output
            assert "[FAIL] docker-up" in output
            assert "4/5 checks passed" in output


class TestLifecycleSetupHelpers(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_setup_worktree_dir_skips_nonexistent_path(self) -> None:
        """_setup_worktree_dir returns early when path doesn't exist."""
        from teatree.core.runners.worktree_provision import _setup_worktree_dir  # noqa: PLC0415

        mock_overlay = MagicMock()
        # Empty path — should return early without calling anything
        _setup_worktree_dir("", MagicMock(), mock_overlay)
        mock_overlay.get_envrc_lines.assert_not_called()
        # Non-existent path
        _setup_worktree_dir("/tmp/does-not-exist-xyz", MagicMock(), mock_overlay)
        mock_overlay.get_envrc_lines.assert_not_called()

    def test_write_env_cache_returns_none_without_path(self) -> None:
        """write_env_cache returns None when worktree has no worktree_path."""
        from teatree.core.worktree_env import write_env_cache  # noqa: PLC0415

        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/250")
        wt = Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={},  # no worktree_path
        )
        assert write_env_cache(wt) is None


class TestLifecycleStart(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_starts_docker_compose_and_transitions(self) -> None:
        """Lifecycle start should provision, run docker compose up -d, and transition to SERVICES_UP."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_path = tmp_path / "worktree"
            wt_path.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/300", variant="acme")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_path)},
                db_name="wt_300_acme",
                state=Worktree.State.PROVISIONED,
            )

            mock_overlay = env_safe_mock_overlay()
            mock_overlay.get_run_commands.return_value = {"backend": "run-backend", "frontend": "run-frontend"}
            mock_overlay.get_pre_run_steps.return_value = []
            mock_overlay.get_envrc_lines.return_value = []
            mock_overlay.get_provision_steps.return_value = []
            mock_overlay.get_post_db_steps.return_value = []
            mock_overlay.get_health_checks.return_value = []
            mock_overlay.get_reset_passwords_command.return_value = None
            mock_overlay.get_compose_file.return_value = "/fake/docker-compose.yml"

            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path

            with (
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                mock_sp.run.return_value = MagicMock(returncode=0)
                call_command("worktree", "start", path=str(wt_path))

            worktree = Worktree.objects.filter(ticket=ticket).first()
            assert worktree is not None
            assert worktree.state == Worktree.State.SERVICES_UP
            # Docker compose was called (down + up)
            docker_calls = [c for c in mock_sp.run.call_args_list if c[0] and "docker" in str(c[0][0])]
            assert len(docker_calls) >= 2

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_skips_worktree_without_compose_file(self) -> None:
        """Start skips worktrees with no compose file (e.g. frontend-only)."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_path = tmp_path / "worktree"
            wt_path.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/302", variant="acme")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_path)},
                db_name="wt_302_acme",
                state=Worktree.State.PROVISIONED,
            )

            mock_overlay = env_safe_mock_overlay()
            mock_overlay.get_run_commands.return_value = {}
            mock_overlay.get_pre_run_steps.return_value = []
            mock_overlay.get_envrc_lines.return_value = []
            mock_overlay.get_provision_steps.return_value = []
            mock_overlay.get_post_db_steps.return_value = []
            mock_overlay.get_health_checks.return_value = []
            mock_overlay.get_reset_passwords_command.return_value = None
            mock_overlay.get_compose_file.return_value = ""

            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path

            with (
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                mock_sp.run.return_value = MagicMock(returncode=0)
                result = call_command("worktree", "start", path=str(wt_path))

            assert result != "error"  # skipped, not failed

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_reports_docker_compose_up_failure(self) -> None:
        """If docker compose up fails, start returns 'error'."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            wt_path = tmp_path / "worktree"
            wt_path.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/301", variant="acme")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_path)},
                db_name="wt_301_acme",
                state=Worktree.State.PROVISIONED,
            )

            mock_overlay = env_safe_mock_overlay()
            mock_overlay.get_run_commands.return_value = {"backend": "run-backend"}
            mock_overlay.get_pre_run_steps.return_value = []
            mock_overlay.get_envrc_lines.return_value = []
            mock_overlay.get_provision_steps.return_value = []
            mock_overlay.get_post_db_steps.return_value = []
            mock_overlay.get_health_checks.return_value = []
            mock_overlay.get_reset_passwords_command.return_value = None
            mock_overlay.get_compose_file.return_value = "/fake/docker-compose.yml"

            mock_config = MagicMock()
            mock_config.user.workspace_dir = tmp_path

            call_count = 0

            def _mock_run(cmd, **kwargs):
                nonlocal call_count
                call_count += 1
                # First call is docker compose down (succeeds), second is up (fails)
                if call_count <= 1:
                    return MagicMock(returncode=0, stderr="")
                return MagicMock(returncode=1, stderr="some error")

            with (
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                mock_sp.run.side_effect = _mock_run
                with pytest.raises(SystemExit):
                    call_command("worktree", "start", path=str(wt_path))


class TestImagePreflight(TestCase):
    """`worktree start` auto-builds compose service images that are missing locally.

    Background: `docker compose up --no-build --pull=never` fails hard on the
    first start of a worktree whose compose override declares a `build:`-only
    service (e.g. an overlay's PDF-renderer sidecar). The fix is a preflight: ask
    compose for each service's resolved image, `docker image inspect` it, and
    `docker compose build` any that are missing before the `up` call. Once
    built, subsequent `up --no-build` calls reuse the local image — code
    changes still rely on volume-mounted source for hot reload.

    Tests assert the call sequence on the patched subprocess so we don't need
    a real Docker daemon.
    """

    def _setup(self, tmp_path: Path) -> tuple[Path, "MagicMock", "MagicMock"]:
        wt_path = tmp_path / "worktree"
        wt_path.mkdir()
        ticket = Ticket.objects.create(
            overlay="test",
            issue_url="https://example.com/issues/483",
            variant="acme",
        )
        Worktree.objects.create(
            overlay="test",
            ticket=ticket,
            repo_path="/tmp/backend",
            branch="feature",
            extra={"worktree_path": str(wt_path)},
            db_name="wt_483_acme",
            state=Worktree.State.PROVISIONED,
        )

        mock_overlay = env_safe_mock_overlay()
        mock_overlay.get_run_commands.return_value = {"backend": "run-backend"}
        mock_overlay.get_pre_run_steps.return_value = []
        mock_overlay.get_envrc_lines.return_value = []
        mock_overlay.get_provision_steps.return_value = []
        mock_overlay.get_post_db_steps.return_value = []
        mock_overlay.get_health_checks.return_value = []
        mock_overlay.get_reset_passwords_command.return_value = None
        mock_overlay.get_compose_file.return_value = "/fake/docker-compose.yml"

        mock_config = MagicMock()
        mock_config.user.workspace_dir = tmp_path
        return wt_path, mock_overlay, mock_config

    @staticmethod
    def _docker_subcommand(cmd: list[str]) -> tuple[str, ...]:
        """Identify the docker subcommand from a recorded argv list.

        Examples: ``docker compose -p x -f y config --format json`` → ``("compose", "config")``;
        ``docker image inspect img:tag`` → ``("image", "inspect")``;
        ``docker compose -p x build svc`` → ``("compose", "build")``.
        """
        if not isinstance(cmd, list) or len(cmd) < 2 or cmd[0] != "docker":
            return ()
        if cmd[1] == "image":
            return ("image", cmd[2]) if len(cmd) >= 3 else ("image",)
        if cmd[1] == "compose":
            i = 2
            while i < len(cmd) and cmd[i] in {"-p", "-f", "--project-name", "--file"}:
                i += 2
            return ("compose", cmd[i]) if i < len(cmd) else ("compose",)
        return (cmd[1],) if len(cmd) >= 2 else ()

    @staticmethod
    def _recorded_subcommands(mock_sp) -> list[tuple[str, ...]]:
        recorded: list[tuple[str, ...]] = []
        for call_args in mock_sp.run.call_args_list:
            cmd = call_args.args[0] if call_args.args else call_args.kwargs.get("args", [])
            shape = TestImagePreflight._docker_subcommand(cmd)
            if shape:
                recorded.append(shape)
        return recorded

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_builds_missing_image_before_up(self) -> None:
        """When compose config declares a build: service whose image is absent, build it first."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path, mock_overlay, mock_config = self._setup(tmp_path)

            compose_config_json = (
                '{"services": {"sidecar": {"image": "myapp-wt483-sidecar:latest", '
                '"build": {"context": "../sidecar"}}, "web": {"image": "myapp-web:cached"}}}'
            )

            def _mock_run(cmd, **kwargs):
                shape = TestImagePreflight._docker_subcommand(cmd)
                if shape == ("compose", "config"):
                    return MagicMock(returncode=0, stdout=compose_config_json, stderr="")
                if shape == ("image", "inspect"):
                    # web image present, sidecar image missing
                    missing = "sidecar" in cmd[-1]
                    return MagicMock(returncode=1 if missing else 0, stdout="", stderr="")
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                mock_sp.run.side_effect = _mock_run
                call_command("worktree", "start", path=str(wt_path))

            recorded = TestImagePreflight._recorded_subcommands(mock_sp)
            # The preflight must run before up: config → image inspect → build → up.
            assert ("compose", "config") in recorded
            assert ("image", "inspect") in recorded
            assert ("compose", "build") in recorded
            assert ("compose", "up") in recorded
            assert recorded.index(("compose", "build")) < recorded.index(("compose", "up"))
            # And `build` must come AFTER we discovered the missing image.
            assert recorded.index(("image", "inspect")) < recorded.index(("compose", "build"))

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_skips_build_when_all_images_present(self) -> None:
        """When every buildable service's image already exists, no `compose build` call is made."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path, mock_overlay, mock_config = self._setup(tmp_path)

            compose_config_json = (
                '{"services": {"sidecar": {"image": "myapp-wt483-sidecar:latest", "build": {"context": "../sidecar"}}}}'
            )

            def _mock_run(cmd, **kwargs):
                shape = TestImagePreflight._docker_subcommand(cmd)
                if shape == ("compose", "config"):
                    return MagicMock(returncode=0, stdout=compose_config_json, stderr="")
                if shape == ("image", "inspect"):
                    return MagicMock(returncode=0, stdout="[]", stderr="")  # present
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                mock_sp.run.side_effect = _mock_run
                call_command("worktree", "start", path=str(wt_path))

            recorded = TestImagePreflight._recorded_subcommands(mock_sp)
            assert ("compose", "build") not in recorded
            assert ("compose", "up") in recorded

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_falls_through_when_config_fails(self) -> None:
        """If `docker compose config` errors, fall through to `up` rather than aborting.

        Compose versions without `--format json`, malformed compose files, or
        permission issues should not prevent `worktree start` from attempting
        `up`. The user's existing flow (build manually or accept the natural
        `up` failure) stays available.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_path, mock_overlay, mock_config = self._setup(tmp_path)

            def _mock_run(cmd, **kwargs):
                shape = TestImagePreflight._docker_subcommand(cmd)
                if shape == ("compose", "config"):
                    return MagicMock(returncode=1, stdout="", stderr="unknown flag: --format")
                return MagicMock(returncode=0, stdout="", stderr="")

            with (
                patch.object(worktree_mod, "get_overlay", return_value=mock_overlay),
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch("teatree.config.load_config", return_value=mock_config),
            ):
                mock_sp.run.side_effect = _mock_run
                call_command("worktree", "start", path=str(wt_path))

            recorded = TestImagePreflight._recorded_subcommands(mock_sp)
            assert ("compose", "build") not in recorded
            assert ("compose", "up") in recorded


class TestLifecycleClean(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_tears_down_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/62")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature",
                extra={"worktree_path": str(wt_dir)},
            )
            wt.provision()
            wt.save()

            with patch.object(utils_run_mod, "subprocess") as mock_sp:
                mock_sp.run.return_value = MagicMock(returncode=0)
                result = cast("str", call_command("worktree", "teardown", path=str(wt_dir)))

            # Teardown folds the old `clean` step — the row is deleted, not reset
            assert not Worktree.objects.filter(pk=wt.pk).exists()
            assert "cleaned" in result.lower()
            assert "/tmp/backend" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_drops_database_on_clean(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/63")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="/tmp/backend",
                branch="feature-db",
                extra={"worktree_path": str(wt_dir)},
            )
            wt.provision()
            wt.save()

            commands_run: list[list[str]] = []

            def _capture(*args: object, **kwargs: object) -> MagicMock:
                if args:
                    commands_run.append(list(args[0]))
                return MagicMock(returncode=0)

            with (
                patch.object(utils_run_mod, "subprocess") as mock_sp,
                patch.object(db_mod, "pg_env", return_value={}),
                patch.object(db_mod, "pg_host", return_value="localhost"),
                patch.object(db_mod, "pg_user", return_value="postgres"),
            ):
                mock_sp.run.side_effect = _capture
                call_command("worktree", "teardown", path=str(wt_dir))

            dropdb_cmds = [c for c in commands_run if "dropdb" in c]
            assert len(dropdb_cmds) == 1
            # provision() generates db_name as wt_{ticket_number}
            assert f"wt_{ticket.ticket_number}" in " ".join(dropdb_cmds[0])


class TestLifecycleStatus(TestCase):
    pass  # No status tests in the original file — placeholder for future tests


class TestLifecycleDiagnose(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_healthy_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            # .git file marks this as a worktree (not a main clone)
            (wt_dir / ".git").write_text("gitdir: /tmp/.git/worktrees/backend")
            cache_dir = tmp_path / ".t3-cache"
            cache_dir.mkdir()
            (cache_dir / ".t3-env.cache").write_text("WT_DB_NAME=wt_120\n")

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/120")
            wt = Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature-120",
                extra={"worktree_path": str(wt_dir)},
                db_name="wt_120",
            )
            wt.provision()
            wt.save()

            with patch.object(utils_run_mod, "subprocess") as mock_sp:
                # Mock docker compose ps (returns running services)
                mock_sp.run.return_value = MagicMock(returncode=0, stdout="backend  running\n")
                result = cast("dict[str, object]", call_command("worktree", "diagnose", path=str(wt_dir)))

            assert result["worktree_dir"] is True
            assert result["env_cache"] is True
            assert result["db_name"] == "wt_120"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_missing_db_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            wt_dir = tmp_path / "backend"
            wt_dir.mkdir()
            (wt_dir / ".git").write_text("gitdir: /tmp/.git/worktrees/backend")

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/121")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature-121",
                extra={"worktree_path": str(wt_dir)},
            )

            with patch.object(utils_run_mod, "subprocess") as mock_sp:
                mock_sp.run.return_value = MagicMock(returncode=0, stdout="")
                result = cast("dict[str, object]", call_command("worktree", "diagnose", path=str(wt_dir)))

            assert result["db_name"] == ""
            assert result["worktree_dir"] is True


@patch("subprocess.run", return_value=MagicMock(returncode=0))
class TestLifecycleSmokeTest(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_returns_health_checks(self, mock_subprocess: MagicMock) -> None:
        result = cast(
            "dict[str, dict[str, object]]",
            call_command("worktree", "smoke-test"),
        )
        assert result["overlay"]["status"] == "ok"
        assert result["database"]["status"] == "ok"
        assert "cli" in result

    @override_settings(**SETTINGS)
    def test_overlay_error(self, mock_subprocess: MagicMock) -> None:
        """smoke-test reports overlay error when loading fails."""

        def _broken_discover() -> dict:
            msg = "broken"
            raise RuntimeError(msg)

        _broken_discover.cache_clear = lambda: None

        with patch.object(overlay_loader_mod, "_discover_overlays", new=_broken_discover):
            result = cast(
                "dict[str, dict[str, object]]",
                call_command("worktree", "smoke-test"),
            )

        assert result["overlay"]["status"] == "error"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_hooks_skipped_when_no_config(self, mock_subprocess: MagicMock) -> None:
        """smoke-test reports hooks skipped when no .pre-commit-config.yaml."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            saved_cwd = Path.cwd()
            try:
                import os  # noqa: PLC0415

                os.chdir(tmp_path)
                env_patch = {k: v for k, v in os.environ.items() if k != "PWD"}
                with patch.dict("os.environ", env_patch, clear=True):
                    result = cast(
                        "dict[str, dict[str, object]]",
                        call_command("worktree", "smoke-test"),
                    )
            finally:
                os.chdir(saved_cwd)
            assert result["hooks"]["status"] == "skipped"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_hooks_ok_with_yaml(self, mock_subprocess: MagicMock) -> None:
        """smoke-test reports hooks OK when yaml parses successfully."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config = tmp_path / ".pre-commit-config.yaml"
            config.write_text("repos: []\n", encoding="utf-8")
            saved_cwd = Path.cwd()
            try:
                import os  # noqa: PLC0415

                os.chdir(tmp_path)
                env_patch = {k: v for k, v in os.environ.items() if k != "PWD"}
                with patch.dict("os.environ", env_patch, clear=True):
                    mock_yaml = MagicMock()
                    with patch("importlib.import_module", return_value=mock_yaml):
                        result = cast(
                            "dict[str, dict[str, object]]",
                            call_command("worktree", "smoke-test"),
                        )
            finally:
                os.chdir(saved_cwd)
            assert result["hooks"]["status"] == "ok"

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_db_error(self, mock_subprocess: MagicMock) -> None:
        """smoke-test reports DB error when query fails."""
        with patch.object(
            Worktree,
            "objects",
            MagicMock(count=MagicMock(side_effect=RuntimeError("DB down"))),
        ):
            result = cast(
                "dict[str, dict[str, object]]",
                call_command("worktree", "smoke-test"),
            )
        assert result["database"]["status"] == "error"


class TestLifecycleDiagram(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_worktree(self) -> None:
        result = cast("str", call_command("worktree", "diagram"))

        assert "stateDiagram-v2" in result
        assert "[*] --> created" in result
        assert "provision()" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_ticket(self) -> None:
        result = cast("str", call_command("worktree", "diagram", model="ticket"))

        assert "stateDiagram-v2" in result
        assert "[*] --> not_started" in result
        assert "scope()" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_task(self) -> None:
        result = cast("str", call_command("worktree", "diagram", model="task"))

        assert "stateDiagram-v2" in result
        assert "pending --> claimed: claim()" in result
        assert "claimed --> completed: complete()" in result
        assert "claimed --> failed: fail()" in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_unknown_model_raises_system_exit_1(self) -> None:
        """#939: an unknown model is a real error — exit 1, message on stderr.

        Regression: `return f"Unknown model..."` exited 0, so a typo in
        `worktree diagram --model` looked like success to headless callers.
        """
        stderr = StringIO()

        with pytest.raises(SystemExit) as exc_info:
            call_command("worktree", "diagram", model="unknown", stderr=stderr)

        assert exc_info.value.code == 1
        assert "Unknown model: unknown" in stderr.getvalue()

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_ticket_lifecycle_diagram(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/d1")
        with patch(
            "teatree.core.selectors.build_ticket_lifecycle_mermaid",
            return_value="mermaid-output",
        ) as mock_build:
            result = cast("str", call_command("worktree", "diagram", ticket=ticket.pk))

        mock_build.assert_called_once_with(ticket.pk)
        assert result == "mermaid-output"


class TestLifecycleVisitPhase(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_creates_session_and_visits_phase(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/vp1")
        result = cast("str", call_command("lifecycle", "visit-phase", str(ticket.pk), "coding"))

        assert "coding" in result
        assert ticket.sessions.count() == 1
        session = ticket.sessions.first()
        assert "coding" in session.visited_phases

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_reuses_existing_session(self) -> None:
        ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/vp2")
        session = Session.objects.create(ticket=ticket)
        session.visit_phase("testing")

        result = cast(
            "str",
            call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="cold-reviewer"),
        )

        assert ticket.sessions.count() == 1
        session.refresh_from_db()
        assert "testing" in session.visited_phases
        assert "reviewing" in session.visited_phases
        assert str(session.pk) in result

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_advances_fsm_alongside_session_visit(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test", issue_url="https://example.com/issues/vp3", state=Ticket.State.NOT_STARTED
        )
        cast("str", call_command("lifecycle", "visit-phase", str(ticket.pk), "scoping"))

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.SCOPED

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_fsm_mismatch_does_not_block_phase_visit(self) -> None:
        ticket = Ticket.objects.create(
            overlay="test", issue_url="https://example.com/issues/vp4", state=Ticket.State.NOT_STARTED
        )
        cast(
            "str",
            call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="cold-reviewer"),
        )

        ticket.refresh_from_db()
        assert ticket.state == Ticket.State.NOT_STARTED
        session = ticket.sessions.first()
        assert "reviewing" in session.visited_phases


# ── Repo discovery in worktree provision ──────────────────────────────


class TestLifecycleRepoDiscovery(TestCase):
    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_discovers_new_repo_in_ticket_dir(self) -> None:
        """A git worktree added manually to the ticket dir gets auto-registered."""
        pytest.skip(
            "Auto-discovery of sibling repos removed in worktree FSM refactor — "
            "the per-worktree command no longer scans the ticket dir",
        )

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_skips_main_clones(self) -> None:
        """Directories with .git as a directory (main clones) are not registered."""
        pytest.skip(
            "Auto-discovery of sibling repos removed in worktree FSM refactor — "
            "the per-worktree command no longer scans the ticket dir",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            ticket_dir = tmp_path / "ticket-456"
            ticket_dir.mkdir()

            existing = ticket_dir / "backend"
            existing.mkdir()

            main_clone = ticket_dir / "main-repo"
            main_clone.mkdir()
            (main_clone / ".git").mkdir()  # directory, not file = main clone

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/96")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(existing)},
            )

            with patch.object(utils_run_mod.subprocess, "run"):
                call_command("worktree", "provision", path=str(existing))

            assert ticket.worktrees.count() == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_skips_non_git_directories(self) -> None:
        """Non-git subdirectories (logs, etc.) are not registered."""
        pytest.skip(
            "Auto-discovery of sibling repos removed in worktree FSM refactor — "
            "the per-worktree command no longer scans the ticket dir",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            ticket_dir = tmp_path / "ticket-789"
            ticket_dir.mkdir()

            existing = ticket_dir / "backend"
            existing.mkdir()

            (ticket_dir / "logs").mkdir()
            (ticket_dir / "notes.txt").touch()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/97")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(existing)},
            )

            with patch.object(utils_run_mod.subprocess, "run"):
                call_command("worktree", "provision", path=str(existing))

            assert ticket.worktrees.count() == 1

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_idempotent_does_not_duplicate(self) -> None:
        """Running setup twice doesn't create duplicate Worktree records."""
        pytest.skip(
            "Auto-discovery of sibling repos removed in worktree FSM refactor — "
            "the per-worktree command no longer scans the ticket dir",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            ticket_dir = tmp_path / "ticket-idem"
            ticket_dir.mkdir()

            existing = ticket_dir / "backend"
            existing.mkdir()

            new_repo = ticket_dir / "frontend"
            new_repo.mkdir()
            (new_repo / ".git").write_text("gitdir: /some/path")

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/98")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(existing)},
            )

            with patch.object(utils_run_mod.subprocess, "run"):
                call_command("worktree", "provision", path=str(existing))
                call_command("worktree", "provision", path=str(existing))

            assert ticket.worktrees.count() == 2

    @_patch_overlays(FULL_OVERLAY)
    @override_settings(**SETTINGS)
    def test_provisions_all_ticket_worktrees(self) -> None:
        """Setup provisions all worktrees for the ticket, not just the resolved one."""
        pytest.skip(
            "Bulk per-ticket provisioning moved to t3 workspace provision — "
            "needs rewrite as call_command('workspace', 'provision', ...) test",
        )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            ticket_dir = tmp_path / "ticket-all"
            ticket_dir.mkdir()

            backend = ticket_dir / "backend"
            backend.mkdir()
            frontend = ticket_dir / "frontend"
            frontend.mkdir()

            ticket = Ticket.objects.create(overlay="test", issue_url="https://example.com/issues/99")
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="backend",
                branch="feature",
                extra={"worktree_path": str(backend)},
            )
            Worktree.objects.create(
                overlay="test",
                ticket=ticket,
                repo_path="frontend",
                branch="feature",
                extra={"worktree_path": str(frontend)},
            )

            with patch.object(utils_run_mod.subprocess, "run"):
                call_command("worktree", "provision", path=str(backend))

            # Both worktrees should be provisioned
            for wt in ticket.worktrees.all():
                wt.refresh_from_db()
                assert wt.state == Worktree.State.PROVISIONED

    def test_register_skips_when_no_ticket(self) -> None:
        """_register_new_repos returns early when worktree has no ticket."""
        pytest.skip("_register_new_repos removed in worktree FSM refactor — needs rewrite")

    def test_register_skips_when_no_worktree_path(self) -> None:
        """_register_new_repos returns early when extra has no worktree_path."""
        pytest.skip("_register_new_repos removed in worktree FSM refactor — needs rewrite")

    def test_register_skips_when_ticket_dir_missing(self) -> None:
        """_register_new_repos returns early when ticket directory doesn't exist."""
        pytest.skip("_register_new_repos removed in worktree FSM refactor — needs rewrite")
