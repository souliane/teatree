"""``DoctorService.make_editable`` — pyproject patching + dev-sources marker.

Lifted verbatim from the former monolithic ``tests/test_cli_doctor.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

import subprocess
from pathlib import Path
from unittest.mock import patch

from teatree.cli.doctor import DoctorService


class TestMakeEditable:
    """``make_editable`` shells out to ``uv``/``git``; those are the boundary mocks."""

    def test_success_patches_pyproject_and_writes_marker(self, tmp_path, capsys):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[tool.uv.sources]\nteatree = { git = "https://example.com", branch = "main" }\n')
        (tmp_path / "manage.py").write_text("")

        success = subprocess.CompletedProcess([], 0)
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=tmp_path),
            patch("subprocess.run", return_value=success),
        ):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))

        assert "now editable" in capsys.readouterr().out
        assert (tmp_path / ".t3-dev-sources").is_file()
        rewritten = pyproject.read_text()
        assert "path =" in rewritten
        assert "editable = true" in rewritten

    def test_falls_back_to_ephemeral_install_without_host_project(self, capsys):
        success = subprocess.CompletedProcess([], 0)
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=None),
            patch("subprocess.run", return_value=success),
        ):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))

        assert "ephemeral" in capsys.readouterr().out

    def test_reports_warn_when_pyproject_has_no_source_entry(self, tmp_path, capsys):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text('[project]\nname = "myproject"\n')
        (tmp_path / "manage.py").write_text("")

        with patch("teatree.cli.doctor._find_host_project_root", return_value=tmp_path):
            DoctorService.make_editable("teatree", Path("/tmp/teatree"))

        assert "uv tool install" in capsys.readouterr().out

    def test_reports_fail_without_host_project_when_install_fails(self, tmp_path):
        failure = subprocess.CompletedProcess([], 1, "", "install failed")
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=None),
            patch("subprocess.run", return_value=failure),
        ):
            DoctorService.make_editable("teatree", tmp_path)

    def test_reports_fail_when_uv_sync_fails(self, tmp_path):
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            '[project]\nname = "test"\n\n[tool.uv.sources]\nteatree = { git = "https://x" }\n',
        )
        failure = subprocess.CompletedProcess([], 1, "", "sync failed")
        with (
            patch("teatree.cli.doctor._find_host_project_root", return_value=tmp_path),
            patch("subprocess.run", return_value=failure),
        ):
            DoctorService.make_editable("teatree", Path("/repos/teatree"))
