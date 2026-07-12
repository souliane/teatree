import os
import subprocess
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.shell import ShellDeniedError, _resolve_shell, build_shell_toolset


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

    def test_pinned_env_still_carries_path_and_home(self, tmp_path: Path) -> None:
        # AH-10: a pinned child env (built through from_options, which merges over
        # os.environ) must still expose PATH/HOME to the spawned shell — a bare
        # replacement would strip them and break every command.
        options = ClaudeAgentOptions(cwd=str(tmp_path), env={"ANTHROPIC_API_KEY": "sk-pinned"})
        cfg = LaneBToolConfig.from_options(options, phase="coding")
        out = _shell(cfg)('printf "PATH=%s HOME=%s KEY=%s" "$PATH" "$HOME" "$ANTHROPIC_API_KEY"')
        assert out.startswith("exit=0")
        # The real ambient PATH survived the merge (non-empty, first entry present).
        assert os.environ["PATH"].split(":")[0] in out
        assert f"HOME={os.environ['HOME']}" in out  # ...and HOME
        assert "KEY=sk-pinned" in out  # ...and the pinned override reached the shell too

    def test_resolves_an_absolute_shell_path(self) -> None:
        # AH-11: the shell is resolved to an absolute path (not the bare "bash" name),
        # so the runner does not assume bash sits first on PATH.
        resolved = _resolve_shell()
        assert Path(resolved).name in {"bash", "sh"}
        # On any dev/CI host at least one POSIX shell is installed, so which() resolves
        # an absolute path; the bare-name fallback only fires when neither is present.
        assert Path(resolved).is_absolute()
