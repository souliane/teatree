import subprocess
from pathlib import Path

import pytest

from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.shell import ShellDeniedError, build_shell_toolset


def _shell(config):
    return build_shell_toolset(config).tools["shell"].function


class TestShellTool:
    def test_runs_command_in_worktree_and_returns_output(self, tmp_path: Path) -> None:
        (tmp_path / "marker.txt").write_text("x")
        out = _shell(LaneBToolConfig(fs_root=tmp_path))("ls")
        assert "marker.txt" in out
        assert out.startswith("exit=0")

    def test_nonzero_exit_is_reported_not_raised(self, tmp_path: Path) -> None:
        out = _shell(LaneBToolConfig(fs_root=tmp_path))("exit 3")
        assert out.startswith("exit=3")

    def test_denylisted_command_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(ShellDeniedError):
            _shell(LaneBToolConfig(fs_root=tmp_path))("rm -rf /")

    def test_timeout_is_enforced(self, tmp_path: Path) -> None:
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_timeout_seconds=0.5)
        with pytest.raises(subprocess.TimeoutExpired):
            _shell(cfg)("sleep 5")

    def test_custom_denylist_entry_matches(self, tmp_path: Path) -> None:
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_denylist=("forbidden",))
        with pytest.raises(ShellDeniedError):
            _shell(cfg)("run forbidden thing")
