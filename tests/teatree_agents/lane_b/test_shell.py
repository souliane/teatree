import os
from pathlib import Path

import pytest

from teatree.agents.harness_options import HarnessOptions
from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.shell import (
    ShellDeniedError,
    ShellTimeoutError,
    _capped,
    _elision_marker,
    _resolve_shell,
    build_shell_toolset,
)


def _shell(config):
    return build_shell_toolset(config).tools["Bash"].function


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
        # Raises ShellTimeoutError (a ToolInputError), NOT the raw
        # subprocess.TimeoutExpired — the gate wrapper only converts a recognized
        # ToolInputError family into a retryable ModelRetry (see test_gating.py's
        # TestToolFailuresReachTheModelInsteadOfKillingTheRun); the bare stdlib
        # exception escaped uncaught and crashed the whole dispatch three times
        # over.
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_timeout_seconds=0.5)
        with pytest.raises(ShellTimeoutError, match="timed out after"):
            _shell(cfg)("sleep 5")

    def test_timeout_message_redacts_a_secret_in_the_command(self, tmp_path: Path) -> None:
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_timeout_seconds=0.5)
        with pytest.raises(ShellTimeoutError) as exc_info:
            _shell(cfg)("sleep 5 # token=super-secret-value")
        assert "super-secret-value" not in str(exc_info.value)
        assert "token=<redacted>" in str(exc_info.value)

    def test_custom_denylist_entry_matches(self, tmp_path: Path) -> None:
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_denylist=("forbidden",))
        with pytest.raises(ShellDeniedError):
            _shell(cfg)("run forbidden thing")

    def test_pinned_env_still_carries_path_and_home(self, tmp_path: Path) -> None:
        # AH-10: a pinned child env (built through from_options, which merges over
        # os.environ) must still expose PATH/HOME to the spawned shell — a bare
        # replacement would strip them and break every command.
        options = HarnessOptions(cwd=str(tmp_path), env={"ANTHROPIC_API_KEY": "sk-pinned"})
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


class TestOversizedOutputIsCapped:
    """The per-request cost of one huge return, bounded head+tail (#4816)."""

    _HUGE = 200_000

    def _huge_output(self, tmp_path: Path, cap: int) -> str:
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_max_output_bytes=cap)
        return _shell(cfg)(f"python3 -c \"print('x' * {self._HUGE})\"")

    def test_a_huge_return_is_bounded_by_the_cap(self, tmp_path: Path) -> None:
        out = self._huge_output(tmp_path, 16 * 1024)
        assert len(out.encode()) <= 16 * 1024 + len("exit=0\n")
        assert len(out) < self._HUGE

    def test_the_default_cap_bounds_a_huge_return(self, tmp_path: Path) -> None:
        out = _shell(LaneBToolConfig(fs_root=tmp_path))(f"python3 -c \"print('x' * {self._HUGE})\"")
        assert len(out) < self._HUGE

    def test_the_elision_names_the_dropped_bytes_and_the_recovery(self, tmp_path: Path) -> None:
        out = self._huge_output(tmp_path, 16 * 1024)
        assert "bytes elided from the middle" in out
        assert "re-run the command narrowed" in out

    def test_head_and_tail_both_survive(self, tmp_path: Path) -> None:
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_max_output_bytes=2048)
        out = _shell(cfg)("python3 -c \"print('HEAD' + 'x' * 100000 + 'TAIL')\"")
        assert "HEAD" in out
        assert "TAIL" in out

    def test_the_exit_status_survives_the_cap(self, tmp_path: Path) -> None:
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_max_output_bytes=1024)
        out = _shell(cfg)("python3 -c \"print('x' * 100000)\"; exit 7")
        assert out.startswith("exit=7")

    def test_a_small_return_is_byte_identical(self, tmp_path: Path) -> None:
        out = _shell(LaneBToolConfig(fs_root=tmp_path))("echo small")
        assert out == "exit=0\nsmall\n"

    def test_zero_disables_the_cap(self, tmp_path: Path) -> None:
        # Never-lockout: an operator can restore the uncapped pre-#4816 return.
        out = self._huge_output(tmp_path, 0)
        assert len(out) > self._HUGE

    def test_the_command_still_runs_to_completion(self, tmp_path: Path) -> None:
        # The cap shrinks what the turn CARRIES; it never fails or truncates the work.
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_max_output_bytes=512)
        out = _shell(cfg)(f"python3 -c \"print('x' * {self._HUGE})\" > big.txt")
        assert out.startswith("exit=0")
        assert (tmp_path / "big.txt").stat().st_size > self._HUGE

    def test_a_cap_below_the_marker_length_returns_only_the_marker(self) -> None:
        assert _capped("x" * self._HUGE, 10) == _elision_marker(self._HUGE)
