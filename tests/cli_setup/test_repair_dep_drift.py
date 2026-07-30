"""Tests for ``repair_dep_drift`` — t3 setup dependency-drift repair.

Lifted verbatim from the former monolithic ``tests/test_cli_setup.py``
(souliane/teatree#443). No behavior change: same assertions, autouse
fixture, and helper, only relocated under a focused package by concern.
"""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from teatree.cli.dep_drift_repair import repair_dep_drift


def _write_pyproject(repo: Path, deps: list[str]) -> Path:
    """Build a minimal ``pyproject.toml`` declaring *deps* under ``[project]``."""
    repo.mkdir(parents=True, exist_ok=True)
    pyproject = repo / "pyproject.toml"
    quoted = ", ".join(f'"{d}"' for d in deps)
    pyproject.write_text(
        f'[project]\nname = "teatree"\nversion = "0"\ndependencies = [{quoted}]\n',
        encoding="utf-8",
    )
    return pyproject


class TestRepairDepDrift:
    """``repair_dep_drift`` repairs the env that actually executes ``t3``.

    The detection (:func:`find_missing_dependencies`) reads the running
    interpreter; the regression these tests guard is the repair targeting a
    *different* env than the one detected — printing ``OK Reinstalled`` while
    the running ``t3`` stays broken (#805).
    """

    @pytest.fixture(autouse=True)
    def _clear_drift_guard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Clear the execv re-exec guard env between cases.

        It leaks into the test process via ``os.environ`` so each case must
        start from a clean slate (the guard test sets it back deliberately).
        """
        monkeypatch.delenv("_T3_DRIFT_REPAIR_ATTEMPTED", raising=False)

    def test_returns_false_when_no_drift(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _write_pyproject(repo, ["django", "httpx"])
        with patch("teatree.cli.dep_drift_repair.find_missing_dependencies", return_value=[]):
            assert repair_dep_drift(repo) is False

    def test_returns_false_when_pyproject_absent(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        assert repair_dep_drift(repo) is False

    def test_warns_on_non_editable_install_points_at_running_python(
        self,
        tmp_path: Path,
        capsys: "pytest.CaptureFixture[str]",
    ) -> None:
        repo = tmp_path / "repo"
        _write_pyproject(repo, ["tomlkit"])
        with (
            patch("teatree.cli.dep_drift_repair.find_missing_dependencies", return_value=["tomlkit"]),
            patch("teatree.cli.dep_drift_repair.editable_source_path", return_value=None),
            patch("teatree.cli.dep_drift_repair.running_python", return_value=Path("/envs/run/bin/python")),
        ):
            assert repair_dep_drift(repo) is False
        out = capsys.readouterr().out
        assert "tomlkit" in out
        # Manual hint must name the *running* interpreter, not a uv tool env.
        assert "/envs/run/bin/python" in out
        assert "uv tool" not in out

    def test_repairs_running_interpreter_when_not_uv_tool_managed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regression (#805): repair a non-uv-tool running env in place.

        A pyenv/venv ``pip install -e`` t3 must be repaired against its own
        interpreter — never via ``uv tool install`` (which targets a
        different, foreign env and leaves the running t3 broken).
        """
        monkeypatch.delenv("_T3_DRIFT_REPAIR_ATTEMPTED", raising=False)
        repo = tmp_path / "repo"
        _write_pyproject(repo, ["tomlkit"])
        source = tmp_path / "main-clone"
        running_py = tmp_path / "pyenv" / "bin" / "python"
        captured: dict[str, object] = {}

        def fake_execv(path: str, argv: list[str]) -> None:
            captured["path"] = path
            captured["argv"] = argv
            raise SystemExit(0)

        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("teatree.cli.dep_drift_repair.find_missing_dependencies", return_value=["tomlkit"]),
            patch("teatree.cli.dep_drift_repair.editable_source_path", return_value=source),
            patch("teatree.cli.dep_drift_repair.running_env_is_uv_tool", return_value=False),
            patch("teatree.cli.dep_drift_repair.running_python", return_value=running_py),
            patch("teatree.cli.dep_drift_repair.running_prefix", return_value=tmp_path / "pyenv"),
            patch("teatree.cli.dep_drift_repair._run_captured", return_value=completed) as mock_run,
            patch("teatree.cli.dep_drift_repair.os.execv", side_effect=fake_execv),
            patch("teatree.cli.dep_drift_repair.sys.argv", ["/pyenv/shims/t3", "setup"]),
            pytest.raises(SystemExit),
        ):
            repair_dep_drift(repo)

        repair_cmd = mock_run.call_args.args[0]
        # The repair MUST target the running interpreter, NOT `uv tool`.
        assert repair_cmd[0] == str(running_py)
        assert repair_cmd[1:4] == ["-m", "pip", "install"]
        assert "-e" in repair_cmd
        assert str(source) in repair_cmd
        assert "uv" not in repair_cmd
        # Re-exec the *running* t3 (argv[0]), not an arbitrary PATH `t3`.
        assert captured["argv"] == ["/pyenv/shims/t3", "setup"]

    def test_uses_uv_tool_when_running_env_is_uv_tool_managed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("_T3_DRIFT_REPAIR_ATTEMPTED", raising=False)
        repo = tmp_path / "repo"
        _write_pyproject(repo, ["tomlkit"])
        source = tmp_path / "main-clone"
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with (
            patch("teatree.cli.dep_drift_repair.find_missing_dependencies", return_value=["tomlkit"]),
            patch("teatree.cli.dep_drift_repair.editable_source_path", return_value=source),
            patch("teatree.cli.dep_drift_repair.running_env_is_uv_tool", return_value=True),
            patch("teatree.cli.dep_drift_repair.shutil.which", return_value="/usr/bin/uv"),
            patch("teatree.cli.dep_drift_repair._run_captured", return_value=completed) as mock_run,
            patch("teatree.cli.dep_drift_repair.os.execv", side_effect=SystemExit(0)),
            patch("teatree.cli.dep_drift_repair.sys.argv", ["/uv/bin/t3", "setup"]),
            pytest.raises(SystemExit),
        ):
            repair_dep_drift(repo)
        install_cmd = mock_run.call_args.args[0]
        assert install_cmd[:3] == ["/usr/bin/uv", "tool", "install"]
        assert "--reinstall" in install_cmd
        assert str(source) in install_cmd

    def test_warns_when_uv_tool_managed_but_uv_missing(
        self,
        tmp_path: Path,
        capsys: "pytest.CaptureFixture[str]",
    ) -> None:
        repo = tmp_path / "repo"
        _write_pyproject(repo, ["tomlkit"])
        source = tmp_path / "main-clone"
        with (
            patch("teatree.cli.dep_drift_repair.find_missing_dependencies", return_value=["tomlkit"]),
            patch("teatree.cli.dep_drift_repair.editable_source_path", return_value=source),
            patch("teatree.cli.dep_drift_repair.running_env_is_uv_tool", return_value=True),
            patch("teatree.cli.dep_drift_repair.shutil.which", return_value=None),
        ):
            assert repair_dep_drift(repo) is False
        assert "uv` is not on PATH" in capsys.readouterr().out

    def test_returns_false_and_prints_manual_fix_when_reinstall_fails(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: "pytest.CaptureFixture[str]",
    ) -> None:
        monkeypatch.delenv("_T3_DRIFT_REPAIR_ATTEMPTED", raising=False)
        repo = tmp_path / "repo"
        _write_pyproject(repo, ["tomlkit"])
        source = tmp_path / "main-clone"
        running_py = tmp_path / "pyenv" / "bin" / "python"
        completed = SimpleNamespace(returncode=1, stdout="", stderr="boom")
        with (
            patch("teatree.cli.dep_drift_repair.find_missing_dependencies", return_value=["tomlkit"]),
            patch("teatree.cli.dep_drift_repair.editable_source_path", return_value=source),
            patch("teatree.cli.dep_drift_repair.running_env_is_uv_tool", return_value=False),
            patch("teatree.cli.dep_drift_repair.running_python", return_value=running_py),
            patch("teatree.cli.dep_drift_repair.running_prefix", return_value=tmp_path / "pyenv"),
            patch("teatree.cli.dep_drift_repair._run_captured", return_value=completed),
            patch("teatree.cli.dep_drift_repair.os.execv") as mock_execv,
        ):
            assert repair_dep_drift(repo) is False
            mock_execv.assert_not_called()
        out = capsys.readouterr().out
        assert "Reinstall failed" in out
        # Loud failure must carry the exact correct command for the running env.
        assert str(running_py) in out
        assert "pip install -e" in out

    def test_guard_warns_with_running_env_not_foreign_uv_tool(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: "pytest.CaptureFixture[str]",
    ) -> None:
        repo = tmp_path / "repo"
        _write_pyproject(repo, ["tomlkit"])
        source = tmp_path / "main-clone"
        running_py = tmp_path / "pyenv" / "bin" / "python"
        monkeypatch.setenv("_T3_DRIFT_REPAIR_ATTEMPTED", "1")
        with (
            patch("teatree.cli.dep_drift_repair.find_missing_dependencies", return_value=["tomlkit"]),
            patch("teatree.cli.dep_drift_repair.editable_source_path", return_value=source),
            patch("teatree.cli.dep_drift_repair.running_python", return_value=running_py),
        ):
            assert repair_dep_drift(repo) is False
        out = capsys.readouterr().out
        assert "already attempted" in out
        assert "tomlkit" in out
        # The post-guard manual fix must name the running interpreter.
        assert str(running_py) in out
        assert "uv tool install" not in out
