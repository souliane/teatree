"""Tests for _extension_points.py — default no-op implementations."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from lib.extension_points import (
    register_defaults,
    ticket_check_deployed,
    ticket_get_mrs,
    ticket_update_external_tracker,
    wt_build_frontend,
    wt_create_mr,
    wt_db_import,
    wt_detect_variant,
    wt_env_extra,
    wt_fetch_ci_errors,
    wt_fetch_failed_tests,
    wt_monitor_pipeline,
    wt_post_db,
    wt_quality_check,
    wt_reset_passwords,
    wt_restore_ci_db,
    wt_run_backend,
    wt_run_frontend,
    wt_run_tests,
    wt_send_review_request,
    wt_services,
    wt_symlinks,
    wt_trigger_e2e,
)
from lib.registry import registered_points


class TestRegisterDefaults:
    def test_registers_all_expected_points(self) -> None:
        register_defaults()
        expected = {
            "wt_symlinks",
            "wt_env_extra",
            "wt_services",
            "wt_db_import",
            "wt_post_db",
            "wt_detect_variant",
            "wt_run_backend",
            "wt_run_frontend",
            "wt_build_frontend",
            "wt_run_tests",
            "wt_create_mr",
            "wt_monitor_pipeline",
            "wt_send_review_request",
            "wt_fetch_failed_tests",
            "wt_restore_ci_db",
            "wt_reset_passwords",
            "wt_trigger_e2e",
            "wt_quality_check",
            "wt_fetch_ci_errors",
            "wt_start_session",
            "ticket_check_deployed",
            "ticket_update_external_tracker",
            "ticket_get_mrs",
        }
        assert expected.issubset(registered_points())


class TestNoOpDefaults:
    """Default implementations are no-ops or return expected defaults."""

    def test_wt_db_import_returns_false(self) -> None:
        assert wt_db_import("db", "var", "/repo") is False

    def test_wt_post_db_is_noop(self) -> None:
        wt_post_db("/some/dir")  # Should not raise

    def test_wt_env_extra_is_noop(self) -> None:
        wt_env_extra("/some/file")  # Should not raise

    def test_wt_run_backend_prints(self, capsys: pytest.CaptureFixture[str]) -> None:
        wt_run_backend()
        assert "Define" in capsys.readouterr().out

    def test_wt_run_frontend_prints(self, capsys: pytest.CaptureFixture[str]) -> None:
        wt_run_frontend()
        assert "Define" in capsys.readouterr().out

    def test_wt_build_frontend_prints(self, capsys: pytest.CaptureFixture[str]) -> None:
        wt_build_frontend()
        assert "Define" in capsys.readouterr().out

    def test_wt_run_tests_prints(self, capsys: pytest.CaptureFixture[str]) -> None:
        wt_run_tests()
        assert "Define" in capsys.readouterr().out

    def test_wt_create_mr_prints(self, capsys: pytest.CaptureFixture[str]) -> None:
        wt_create_mr()
        assert "Define" in capsys.readouterr().out

    def test_wt_monitor_pipeline_prints(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wt_monitor_pipeline()
        assert "Define" in capsys.readouterr().out

    def test_wt_send_review_request_prints(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wt_send_review_request()
        assert "Define" in capsys.readouterr().out

    def test_wt_fetch_failed_tests_prints(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wt_fetch_failed_tests()
        assert "Define" in capsys.readouterr().out

    def test_wt_restore_ci_db_prints(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wt_restore_ci_db()
        assert "Define" in capsys.readouterr().out

    def test_wt_reset_passwords_prints(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wt_reset_passwords()
        assert "Define" in capsys.readouterr().out

    def test_wt_trigger_e2e_prints(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wt_trigger_e2e()
        assert "Define" in capsys.readouterr().out

    def test_wt_quality_check_prints(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wt_quality_check()
        assert "Define" in capsys.readouterr().out

    def test_wt_fetch_ci_errors_prints(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        wt_fetch_ci_errors()
        assert "Define" in capsys.readouterr().out

    def test_ticket_check_deployed_returns_false(self) -> None:
        assert ticket_check_deployed("1234", []) is False

    def test_ticket_update_external_tracker_returns_false(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert ticket_update_external_tracker("1234", "Doing", "org/repo") is False
        assert "No external tracker" in capsys.readouterr().out

    def test_ticket_get_mrs_returns_results(self) -> None:
        mr = {"iid": 1, "web_url": "https://example.com/mr/1"}
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout=json.dumps([mr])),
        ):
            result = ticket_get_mrs("branch", ["repo1"])
        assert len(result) == 1
        assert result[0]["project_path"] == "repo1"

    def test_ticket_get_mrs_skips_failures(self) -> None:
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=1, stdout=""),
        ):
            assert ticket_get_mrs("branch", ["repo1"]) == []

    def test_ticket_get_mrs_skips_empty(self) -> None:
        with patch(
            "subprocess.run",
            return_value=MagicMock(returncode=0, stdout="[]"),
        ):
            assert ticket_get_mrs("branch", ["repo1"]) == []


class TestWtDetectVariant:
    def test_returns_explicit(self) -> None:
        assert wt_detect_variant("acme") == "acme"

    def test_returns_env_var(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("WT_VARIANT", "globex")
        assert wt_detect_variant() == "globex"

    def test_reads_from_ticket_env_worktree(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(ticket_dir / "my-project")
        monkeypatch.delenv("WT_VARIANT", raising=False)

        envwt = ticket_dir / ".env.worktree"
        envwt.write_text("WT_VARIANT=initech\n")

        assert wt_detect_variant() == "initech"

    def test_returns_empty_when_nothing(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(workspace)
        monkeypatch.delenv("WT_VARIANT", raising=False)

        assert wt_detect_variant() == ""

    def test_reads_from_cwd_env_worktree(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Falls back to .env.worktree in CWD when not in a ticket dir."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("WT_VARIANT", raising=False)

        (tmp_path / ".env.worktree").write_text("WT_VARIANT=initech\n")

        assert wt_detect_variant() == "initech"

    def test_falls_through_ticket_dir_to_cwd(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Branch 127->130: ticket dir exists but .env.worktree has no WT_VARIANT."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(ticket_dir / "my-project")
        monkeypatch.delenv("WT_VARIANT", raising=False)

        # Create .env.worktree in ticket dir WITHOUT WT_VARIANT
        (ticket_dir / ".env.worktree").write_text("SOMETHING_ELSE=foo\n")

        # Create .env.worktree in CWD with WT_VARIANT
        cwd_dir = ticket_dir / "my-project"
        (cwd_dir / ".env.worktree").write_text("WT_VARIANT=fromcwd\n")

        assert wt_detect_variant() == "fromcwd"


class TestWtSymlinks:
    def test_creates_venv_symlink(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        main.mkdir()
        (main / ".venv").mkdir()

        wt = tmp_path / "wt"
        wt.mkdir()

        wt_symlinks(str(wt), str(main))
        assert (wt / ".venv").is_symlink()

    def test_replicates_symlinks_from_main(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        main.mkdir()
        target = tmp_path / "some-target"
        target.touch()
        (main / "link").symlink_to(target)

        wt = tmp_path / "wt"
        wt.mkdir()

        wt_symlinks(str(wt), str(main))
        assert (wt / "link").is_symlink()

    def test_skips_real_file_in_worktree(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        main.mkdir()
        target = tmp_path / "some-target"
        target.touch()
        (main / "link").symlink_to(target)

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "link").write_text("real content")

        wt_symlinks(str(wt), str(main))
        assert not (wt / "link").is_symlink()  # Preserved as real file

    def test_creates_node_modules_symlink(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        main.mkdir()
        (main / "node_modules").mkdir()

        wt = tmp_path / "wt"
        wt.mkdir()

        wt_symlinks(str(wt), str(main))
        assert (wt / "node_modules").is_symlink()

    def test_skips_node_modules_when_exists(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        main.mkdir()
        (main / "node_modules").mkdir()

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "node_modules").mkdir()  # Already exists

        wt_symlinks(str(wt), str(main))
        assert not (wt / "node_modules").is_symlink()

    def test_replaces_stale_symlink(self, tmp_path: Path) -> None:
        """Stale symlink in wt is replaced by main's symlink target."""
        main = tmp_path / "main"
        main.mkdir()
        target = tmp_path / "target"
        target.touch()
        (main / "link").symlink_to(target)

        wt = tmp_path / "wt"
        wt.mkdir()
        # Create a stale symlink pointing to non-existent target
        (wt / "link").symlink_to(tmp_path / "old-target")

        wt_symlinks(str(wt), str(main))
        assert (wt / "link").is_symlink()

    def test_handles_oserror_on_all_symlinks(self, tmp_path: Path) -> None:
        """OSError during symlink creation is silently ignored."""
        main = tmp_path / "main"
        main.mkdir()
        target = tmp_path / "target"
        target.touch()
        (main / "link").symlink_to(target)
        (main / ".venv").mkdir()
        (main / "node_modules").mkdir()

        wt = tmp_path / "wt"
        wt.mkdir()

        def always_fail(*_args: object, **_kwargs: object) -> None:
            msg = "Permission denied"
            raise OSError(msg)

        with patch("os.symlink", side_effect=always_fail):
            wt_symlinks(str(wt), str(main))  # Should not raise
        # Nothing got symlinked due to errors
        assert not (wt / "link").exists()
        assert not (wt / ".venv").exists()
        assert not (wt / "node_modules").exists()

    def test_backs_up_old_envrc(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        main.mkdir()

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / ".envrc").write_text("old content")

        wt_symlinks(str(wt), str(main))
        assert (wt / ".envrc.bak").is_file()


class TestWtServices:
    def test_calls_docker_compose(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        main.mkdir()
        (main / "docker-compose.yml").touch()

        with patch("subprocess.run") as mock_run:
            wt_services(str(main))
            assert mock_run.called
            args = mock_run.call_args.args[0]
            assert "docker" in args
            assert "compose" in args

    def test_includes_override_file_when_present(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        main.mkdir()
        (main / "docker-compose.yml").touch()

        wt = tmp_path / "wt"
        wt.mkdir()
        (wt / "docker-compose.override.yml").touch()

        with patch("subprocess.run") as mock_run:
            wt_services(str(main), wt_dir=str(wt))
            args = mock_run.call_args.args[0]
            f_indices = [i for i, a in enumerate(args) if a == "-f"]
            assert len(f_indices) == 2
            assert args[f_indices[1] + 1] == str(wt / "docker-compose.override.yml")

    def test_skips_when_no_compose(self, tmp_path: Path) -> None:
        main = tmp_path / "main"
        main.mkdir()
        # No docker-compose.yml

        with patch("subprocess.run") as mock_run:
            wt_services(str(main))
            assert not mock_run.called
