"""PR-30 bridge exit-code contract.

A bridged child's exit code propagates faithfully as ``SystemExit(returncode)``
with no traceback, so a machine front-end (Pi / CI) can branch on it.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from teatree.cli import overlay
from teatree.utils.run import CommandFailedError


class TestFaithfulChildExit:
    def test_managepy_reraises_child_code_as_systemexit(self) -> None:
        err = CommandFailedError(["python", "-m", "teatree", "ticket", "transition"], 2, "", "No such option")
        with patch.object(overlay, "run_streamed", side_effect=err), pytest.raises(SystemExit) as exc:
            overlay.managepy(None, "ticket", "transition", "--bad")
        assert exc.value.code == 2

    def test_managepy_core_reraises_child_code_as_systemexit(self) -> None:
        err = CommandFailedError(["python", "-m", "teatree", "followup", "sync"], 1, "", "boom")
        with (
            patch.object(overlay, "_overlay_project_env", return_value=None),
            patch.object(overlay, "run_streamed", side_effect=err),
            pytest.raises(SystemExit) as exc,
        ):
            overlay.managepy_core("followup", "sync")
        assert exc.value.code == 1

    def test_success_does_not_raise(self) -> None:
        with patch.object(overlay, "run_streamed", return_value=0):
            overlay.managepy(None, "queue", "status")  # no raise on clean exit


_DJANGO_MISSING = "ModuleNotFoundError: No module named 'django.apps'"


class TestABrokenProjectVenvIsNamed:
    def test_managepy_names_the_venv_and_its_repair_then_exits_with_the_child_code(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        err = CommandFailedError(["uv", "run", "python", "manage.py"], 3, "", "")
        with (
            patch.object(overlay, "project_env_is_drivable", return_value=True),
            patch.object(overlay, "run_streamed", side_effect=err),
            patch.object(overlay, "project_env_import_error", return_value=_DJANGO_MISSING),
            pytest.raises(SystemExit) as exc,
        ):
            overlay.managepy(tmp_path, "tasks", "list")
        stderr = capsys.readouterr().err
        assert exc.value.code == 3
        assert str(tmp_path / ".venv") in stderr
        assert _DJANGO_MISSING in stderr
        assert stderr.count(f"uv sync --directory {tmp_path} --reinstall") == 1

    def test_managepy_core_names_the_overlay_project_venv(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        err = CommandFailedError(["uv", "run", "python", "-m", "teatree"], 1, "", "")
        with (
            patch.object(overlay, "_overlay_project_env", return_value=tmp_path),
            patch.object(overlay, "run_streamed", side_effect=err),
            patch.object(overlay, "project_env_import_error", return_value=_DJANGO_MISSING),
            pytest.raises(SystemExit) as exc,
        ):
            overlay.managepy_core("followup", "sync", overlay_name="t3-acme")
        assert exc.value.code == 1
        assert f"uv sync --directory {tmp_path} --reinstall" in capsys.readouterr().err

    def test_a_healthy_venv_adds_nothing_to_the_child_failure(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "manage.py").write_text("", encoding="utf-8")
        err = CommandFailedError(["uv", "run", "python", "manage.py"], 1, "", "")
        with (
            patch.object(overlay, "project_env_is_drivable", return_value=True),
            patch.object(overlay, "run_streamed", side_effect=err),
            patch.object(overlay, "project_env_import_error", return_value=None),
            pytest.raises(SystemExit),
        ):
            overlay.managepy(tmp_path, "tasks", "list")
        assert capsys.readouterr().err == ""
