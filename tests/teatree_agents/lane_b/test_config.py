from pathlib import Path

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

    def test_from_options_threads_env(self, tmp_path: Path) -> None:
        options = ClaudeAgentOptions(cwd=str(tmp_path), env={"FOO": "bar"})
        assert LaneBToolConfig.from_options(options).shell_env == {"FOO": "bar"}

    def test_defaults_carry_a_denylist_and_timeout(self) -> None:
        config = LaneBToolConfig()
        assert config.shell_denylist
        assert config.shell_timeout_seconds > 0
