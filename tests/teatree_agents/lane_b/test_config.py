import os
from pathlib import Path

import pytest

from teatree.agents.harness_options import HarnessOptions
from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.gating import DEFAULT_MAX_DENIALS


class TestLaneBToolConfig:
    def test_from_options_uses_cwd_as_fs_root(self, tmp_path: Path) -> None:
        options = HarnessOptions(cwd=str(tmp_path))
        config = LaneBToolConfig.from_options(options, phase="coding")
        assert config.fs_root == tmp_path
        assert config.phase == "coding"

    def test_from_options_no_cwd_leaves_fs_root_none(self) -> None:
        config = LaneBToolConfig.from_options(HarnessOptions())
        assert config.fs_root is None
        assert config.phase == ""

    def test_from_options_merges_env_over_os_environ(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # AH-10: a pinned child env is MERGED over os.environ, not a bare replacement —
        # otherwise the subprocess env= would strip PATH/HOME from every shell.
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", str(tmp_path))
        options = HarnessOptions(cwd=str(tmp_path), env={"ANTHROPIC_API_KEY": "sk-pinned"})
        shell_env = LaneBToolConfig.from_options(options).shell_env
        assert shell_env["ANTHROPIC_API_KEY"] == "sk-pinned"  # the override rode through
        assert shell_env["PATH"] == "/usr/bin:/bin"  # ...without stripping PATH
        assert shell_env["HOME"] == str(tmp_path)  # ...or HOME

    def test_from_options_no_override_leaves_env_empty_to_inherit_ambient(self, tmp_path: Path) -> None:
        # No pinned override → shell_env empty so the Shell tool inherits the ambient
        # env (env=None), byte-identical to before the credential port.
        assert LaneBToolConfig.from_options(HarnessOptions(cwd=str(tmp_path))).shell_env == {}

    def test_from_options_override_wins_over_a_conflicting_ambient_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ambient")
        options = HarnessOptions(env={"ANTHROPIC_API_KEY": "sk-pinned"})
        assert LaneBToolConfig.from_options(options).shell_env["ANTHROPIC_API_KEY"] == "sk-pinned"
        assert os.environ["ANTHROPIC_API_KEY"] == "sk-ambient"  # the process env is untouched

    def test_defaults_carry_a_denylist_and_timeout(self) -> None:
        config = LaneBToolConfig()
        assert config.shell_denylist
        assert config.shell_timeout_seconds > 0


class TestShellExplorationRetryBudget:
    """architectural_review et al. get a wider shell retry budget.

    A phase whose own contract is "walk the tree via shell" hit pydantic-ai's
    per-tool retry ceiling and HardDenyToolset's cumulative denial cap on the very
    next corrective retry, aborting the whole dispatch — these properties widen
    both for that phase family, derived from ``phase`` so direct construction and
    ``from_options`` can never disagree.
    """

    @pytest.mark.parametrize(
        "phase", ["architectural_review", "bughunt", "dogfood_smoke", "eval_local", "backlog_sweep"]
    )
    def test_exploration_phase_widens_both_ceilings(self, phase: str) -> None:
        config = LaneBToolConfig(phase=phase)
        assert config.shell_tool_retries is not None
        assert config.shell_tool_retries > 1
        assert config.max_denials > DEFAULT_MAX_DENIALS

    @pytest.mark.parametrize("phase", ["coding", "reviewing", "planning", "shipping", ""])
    def test_other_phase_keeps_the_tight_default(self, phase: str) -> None:
        config = LaneBToolConfig(phase=phase)
        assert config.shell_tool_retries is None  # inherits pydantic-ai's own default unchanged
        assert config.max_denials == DEFAULT_MAX_DENIALS

    def test_from_options_derives_the_same_budget_as_direct_construction(self, tmp_path: Path) -> None:
        # The budget is a property of `phase`, not a field `from_options` fills in
        # separately — so the two construction paths can never disagree.
        via_options = LaneBToolConfig.from_options(HarnessOptions(cwd=str(tmp_path)), phase="architectural_review")
        direct = LaneBToolConfig(fs_root=tmp_path, phase="architectural_review")
        assert via_options.shell_tool_retries == direct.shell_tool_retries
        assert via_options.max_denials == direct.max_denials
