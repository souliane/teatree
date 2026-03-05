"""Tests for frameworks/django.py — Django framework plugin."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from frameworks.django import (
    register_django,
    wt_env_extra,
    wt_post_db,
    wt_run_backend,
    wt_run_tests,
)
from lib.registry import get


class TestWtEnvExtra:
    def test_detects_settings_module(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        manage = workspace / "my-project" / "manage.py"
        # The regex matches: DJANGO_SETTINGS_MODULE...then captures next quoted string
        # Direct assignment style works: DJANGO_SETTINGS_MODULE = "value"
        manage.write_text(
            '#!/usr/bin/env python\nimport os\nDJANGO_SETTINGS_MODULE = "myapp.settings"\n',
        )

        envfile = workspace / ".env.test"
        envfile.write_text("")

        wt_env_extra(str(envfile))
        content = envfile.read_text()
        assert "DJANGO_SETTINGS_MODULE=myapp.settings" in content
        assert "POSTGRES_DB=${WT_DB_NAME}" in content

    def test_writes_postgres_db_even_without_manage(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        ws = tmp_path / "workspace"
        ws.mkdir()
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws))

        envfile = tmp_path / ".env.test"
        envfile.write_text("")

        wt_env_extra(str(envfile))
        content = envfile.read_text()
        assert "POSTGRES_DB=${WT_DB_NAME}" in content


class TestWtPostDb:
    def test_runs_migrate_and_createsuperuser(self) -> None:
        with patch("frameworks.django.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            wt_post_db("/workspace/my-project")

            calls = mock_run.call_args_list
            assert len(calls) == 2
            assert "migrate" in calls[0].args[0]
            assert "createsuperuser" in calls[1].args[0]


class TestWtRunTests:
    def test_uses_pytest_when_available(self) -> None:
        with (
            patch("shutil.which", return_value="/usr/bin/pytest"),
            patch("subprocess.run") as mock_run,
        ):
            wt_run_tests("tests/test_foo.py")
            args = mock_run.call_args.args[0]
            assert args[0] == "pytest"
            assert "tests/test_foo.py" in args

    def test_falls_back_to_manage_test(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch("subprocess.run") as mock_run,
        ):
            wt_run_tests("tests/test_foo.py")
            args = mock_run.call_args.args[0]
            assert "manage.py" in args
            assert "test" in args


class TestWtRunBackend:
    def test_starts_docker_and_runserver(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(ticket_dir / "my-project")

        # Create docker-compose.yml in main repo
        (workspace / "my-project" / "docker-compose.yml").touch()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            wt_run_backend("8042")

        calls = mock_run.call_args_list
        # First call: docker compose up
        assert "docker" in calls[0].args[0]
        # Second call: runserver
        assert "runserver" in calls[1].args[0]
        assert "0.0.0.0:8042" in calls[1].args[0]

    def test_skips_docker_without_compose(
        self,
        workspace: Path,
        ticket_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        monkeypatch.chdir(ticket_dir / "my-project")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            wt_run_backend()

        # Only runserver call (no docker compose)
        assert mock_run.call_count == 1
        assert "runserver" in mock_run.call_args.args[0]

    def test_handles_resolve_error(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If resolve_context fails, still runs runserver."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(tmp_path / "ws"))
        monkeypatch.chdir(tmp_path)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            wt_run_backend("8000")

        assert "runserver" in mock_run.call_args.args[0]


class TestWtEnvExtraLoopEdgeCases:
    def test_no_manage_py_found_in_any_dir(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Branch 23->21: workspace dirs exist but none have manage.py."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        (ws / "some-repo").mkdir()
        (ws / "some-repo" / ".git").mkdir()
        # No manage.py anywhere
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(ws))

        envfile = tmp_path / ".env.test"
        envfile.write_text("")

        wt_env_extra(str(envfile))
        content = envfile.read_text()
        assert "DJANGO_SETTINGS_MODULE" not in content
        assert "POSTGRES_DB=${WT_DB_NAME}" in content

    def test_manage_py_without_settings_module(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Branch 31->36: manage.py exists but no DJANGO_SETTINGS_MODULE pattern."""
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        manage = workspace / "my-project" / "manage.py"
        manage.write_text("#!/usr/bin/env python\nimport os\nos.system('hello')\n")

        envfile = workspace / ".env.test"
        envfile.write_text("")

        wt_env_extra(str(envfile))
        content = envfile.read_text()
        assert "DJANGO_SETTINGS_MODULE" not in content
        assert "POSTGRES_DB=${WT_DB_NAME}" in content


class TestWtEnvExtraOSError:
    def test_handles_oserror_reading_manage_py(
        self,
        workspace: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("T3_WORKSPACE_DIR", str(workspace))
        envfile = workspace / ".env.test"
        envfile.write_text("")

        # Make manage.py unreadable at the filesystem level
        manage_py = workspace / "my-project" / "manage.py"
        manage_py.chmod(0o000)
        try:
            wt_env_extra(str(envfile))
        finally:
            manage_py.chmod(0o644)

        # Should still write POSTGRES_DB
        assert "POSTGRES_DB" in envfile.read_text()


class TestRegisterDjango:
    def test_registers_at_framework_layer(self) -> None:
        register_django()
        # Framework layer should be wt_post_db from django module
        handler = get("wt_post_db")
        assert handler.__module__ == "frameworks.django"
