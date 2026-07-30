"""Tests for ``ToolInstaller.ensure_installed`` — t3 self-install / editable repair.

Lifted verbatim from the former monolithic ``tests/test_cli_setup.py``
(souliane/teatree#443). No behavior change: same assertions and helpers,
only relocated under a focused package by concern.
"""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import patch

from teatree.cli.setup.tool_installer import ToolInstaller

if TYPE_CHECKING:
    import pytest


def _install_run_side_effect(
    uv_tools_dir: Path,
    *,
    install_returncode: int = 0,
    install_stderr: str = "",
) -> Callable[..., SimpleNamespace]:
    """Build a ``subprocess.run`` side effect covering ``uv tool dir`` + install."""

    def side_effect(cmd: list[str], *args: object, **kwargs: object) -> SimpleNamespace:
        if cmd[:3] == ["/usr/bin/uv", "tool", "dir"]:
            stdout = "" if "--bin" in cmd else f"{uv_tools_dir}\n"
            return SimpleNamespace(returncode=0, stderr="", stdout=stdout)
        if cmd[:3] == ["/usr/bin/uv", "tool", "install"]:
            return SimpleNamespace(returncode=install_returncode, stderr=install_stderr, stdout="")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    return side_effect


def _which_t3_and_uv(name: str) -> str | None:
    return {"t3": "/usr/local/bin/t3", "uv": "/usr/bin/uv"}.get(name)


class TestEnsureT3Installed:
    def test_skips_when_editable_source_exists(self, tmp_path: Path) -> None:
        uv_tools_dir = tmp_path / "uv-tools"
        teatree_tool = uv_tools_dir / "teatree"
        teatree_tool.mkdir(parents=True)
        editable_source = tmp_path / "main-clone"
        editable_source.mkdir()
        (teatree_tool / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{ name = "teatree", editable = "{editable_source}" }}]\n'
        )

        with (
            patch("teatree.cli.setup.tool_installer.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=_install_run_side_effect(uv_tools_dir)) as mock_run,
        ):
            mock_which.side_effect = _which_t3_and_uv
            assert ToolInstaller(editable_source).ensure_installed() is True
            # Only the `uv tool dir` receipt lookup — no install invoked.
            install_calls = [c for c in mock_run.call_args_list if c[0][0][:3] == ["/usr/bin/uv", "tool", "install"]]
            assert install_calls == []

    def test_skips_when_install_is_non_editable(self, tmp_path: Path) -> None:
        uv_tools_dir = tmp_path / "uv-tools"
        teatree_tool = uv_tools_dir / "teatree"
        teatree_tool.mkdir(parents=True)
        (teatree_tool / "uv-receipt.toml").write_text('[tool]\nrequirements = [{ name = "teatree" }]\n')

        with (
            patch("teatree.cli.setup.tool_installer.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=_install_run_side_effect(uv_tools_dir)) as mock_run,
        ):
            mock_which.side_effect = _which_t3_and_uv
            assert ToolInstaller(tmp_path / "main-clone").ensure_installed() is True
            install_calls = [c for c in mock_run.call_args_list if c[0][0][:3] == ["/usr/bin/uv", "tool", "install"]]
            assert install_calls == []

    def test_reinstalls_when_editable_source_missing(self, tmp_path: Path) -> None:
        """Stale editable install (worktree deleted) must be repaired from the main clone."""
        uv_tools_dir = tmp_path / "uv-tools"
        teatree_tool = uv_tools_dir / "teatree"
        teatree_tool.mkdir(parents=True)
        deleted_worktree = tmp_path / "deleted-worktree"
        (teatree_tool / "uv-receipt.toml").write_text(
            f'[tool]\nrequirements = [{{ name = "teatree", editable = "{deleted_worktree}" }}]\n'
        )
        main_clone = tmp_path / "main-clone"
        main_clone.mkdir()

        with (
            patch("teatree.cli.setup.tool_installer.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=_install_run_side_effect(uv_tools_dir)) as mock_run,
        ):
            mock_which.side_effect = _which_t3_and_uv
            assert ToolInstaller(main_clone).ensure_installed() is True
            install_calls = [c for c in mock_run.call_args_list if c[0][0][:3] == ["/usr/bin/uv", "tool", "install"]]
            assert len(install_calls) == 1
            args = install_calls[0][0][0]
            assert "--force" in args
            assert "--editable" in args
            assert str(main_clone) in args

    def test_returns_false_when_uv_missing(self, tmp_path: Path) -> None:
        with patch("teatree.cli.setup.tool_installer.shutil.which", return_value=None):
            assert ToolInstaller(tmp_path).ensure_installed() is False

    def test_returns_true_when_t3_on_path_without_uv(self, tmp_path: Path) -> None:
        """Pipx or other non-uv installs are respected — we only touch uv tool installs."""
        with patch("teatree.cli.setup.tool_installer.shutil.which") as mock_which:
            mock_which.side_effect = lambda name: "/usr/local/bin/t3" if name == "t3" else None
            assert ToolInstaller(tmp_path).ensure_installed() is True

    def test_installs_editable_when_t3_missing(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        uv_tools_dir = tmp_path / "uv-tools"
        uv_tools_dir.mkdir()
        with (
            patch("teatree.cli.setup.tool_installer.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=_install_run_side_effect(uv_tools_dir)) as mock_run,
        ):
            mock_which.side_effect = lambda name: "/usr/bin/uv" if name == "uv" else None
            assert ToolInstaller(repo).ensure_installed() is True
            install_calls = [c for c in mock_run.call_args_list if c[0][0][:3] == ["/usr/bin/uv", "tool", "install"]]
            assert len(install_calls) == 1
            args = install_calls[0][0][0]
            assert "--force" in args
            assert "--editable" in args
            assert str(repo) in args

    def test_returns_false_on_install_failure(self, tmp_path: Path) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        uv_tools_dir = tmp_path / "uv-tools"
        uv_tools_dir.mkdir()
        side_effect = _install_run_side_effect(uv_tools_dir, install_returncode=1, install_stderr="boom")
        with (
            patch("teatree.cli.setup.tool_installer.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=side_effect),
        ):
            mock_which.side_effect = lambda name: "/usr/bin/uv" if name == "uv" else None
            assert ToolInstaller(repo).ensure_installed() is False

    def test_prints_shell_rc_hint_when_still_not_on_path(
        self,
        tmp_path: Path,
        capsys: "pytest.CaptureFixture[str]",
    ) -> None:
        repo = tmp_path / "teatree"
        repo.mkdir()
        bin_dir = tmp_path / "uv-bin"
        bin_dir.mkdir()

        def mock_run_side_effect(cmd: list[str], *args: object, **kwargs: object) -> SimpleNamespace:
            stdout = f"{bin_dir}\n" if cmd[:3] == ["/usr/bin/uv", "tool", "dir"] else ""
            return SimpleNamespace(returncode=0, stderr="", stdout=stdout)

        with (
            patch("teatree.cli.setup.tool_installer.shutil.which") as mock_which,
            patch("teatree.utils.run.subprocess.run", side_effect=mock_run_side_effect),
        ):
            mock_which.side_effect = lambda name: "/usr/bin/uv" if name == "uv" else None
            ToolInstaller(repo).ensure_installed()

        out = capsys.readouterr().out
        assert str(bin_dir) in out
        assert "is not on your PATH" in out
        assert 'export PATH="' in out
