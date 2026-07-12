import os
from pathlib import Path

import pytest
from claude_agent_sdk import ClaudeAgentOptions

from teatree.agents.lane_b.config import LaneBToolConfig


class TestLaneBToolConfig:
    def test_from_options_uses_cwd_as_fs_root(self, tmp_path: Path) -> None:
        options = ClaudeAgentOptions(cwd=str(tmp_path))
        config = LaneBToolConfig.from_options(options, phase="coding")
        assert config.fs_root == tmp_path
        assert config.phase == "coding"

    def test_from_options_no_cwd_leaves_fs_root_none(self) -> None:
        config = LaneBToolConfig.from_options(ClaudeAgentOptions())
        assert config.fs_root is None
        assert config.phase == ""

    def test_from_options_merges_env_over_os_environ(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # AH-10: a pinned child env is MERGED over os.environ, not a bare replacement —
        # otherwise the subprocess env= would strip PATH/HOME from every shell.
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", str(tmp_path))
        options = ClaudeAgentOptions(cwd=str(tmp_path), env={"ANTHROPIC_API_KEY": "sk-pinned"})
        shell_env = LaneBToolConfig.from_options(options).shell_env
        assert shell_env["ANTHROPIC_API_KEY"] == "sk-pinned"  # the override rode through
        assert shell_env["PATH"] == "/usr/bin:/bin"  # ...without stripping PATH
        assert shell_env["HOME"] == str(tmp_path)  # ...or HOME

    def test_from_options_no_override_leaves_env_empty_to_inherit_ambient(self, tmp_path: Path) -> None:
        # No pinned override → shell_env empty so the Shell tool inherits the ambient
        # env (env=None), byte-identical to before the credential port.
        assert LaneBToolConfig.from_options(ClaudeAgentOptions(cwd=str(tmp_path))).shell_env == {}

    def test_from_options_override_wins_over_a_conflicting_ambient_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient")
        options = ClaudeAgentOptions(env={"ANTHROPIC_API_KEY": "sk-pinned"})
        assert LaneBToolConfig.from_options(options).shell_env["ANTHROPIC_API_KEY"] == "sk-pinned"
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ambient"  # the process env is untouched

    def test_defaults_carry_a_denylist_and_timeout(self) -> None:
        config = LaneBToolConfig()
        assert config.shell_denylist
        assert config.shell_timeout_seconds > 0
