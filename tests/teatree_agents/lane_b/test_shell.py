import asyncio
import os
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import teatree.agents.lane_b.shell as shell_module
from teatree.agents.harness_options import HarnessOptions
from teatree.agents.lane_b.config import LaneBToolConfig
from teatree.agents.lane_b.filesystem import build_filesystem_toolset
from teatree.agents.lane_b.shell import (
    ShellDeniedError,
    ShellTimeoutError,
    _OutputKeeper,
    _resolve_shell,
    build_shell_toolset,
)
from tests._process_liveness import (
    GATED_FORKING_COMMAND,
    HOLDS_THE_PIPES_FROM_ANOTHER_SESSION,
    is_gone_within,
    is_running,
    release_gate,
    spawn_held_at_its_pipes,
    written_pids,
)


def _bash(config):
    return build_shell_toolset(config).tools["Bash"].function


def _shell(config):
    bash = _bash(config)
    return lambda command: asyncio.run(bash(command))


_GIT = shutil.which("git") or "git"
_FORTY_KB = "head -c 20000 /dev/zero | tr '\\0' a; head -c 20000 /dev/zero | tr '\\0' b"
_THIRTY_TWO_KIB = 32 * 1024


class TestShellTool:
    def test_native_cache_expiry_note_follows_slow_command(self, tmp_path: Path) -> None:
        cfg = LaneBToolConfig(fs_root=tmp_path, cache_lifetime_seconds=0.05)
        with patch.object(shell_module, "monotonic", side_effect=[0.0, 0.1]):
            out = _shell(cfg)("printf done")
        assert out.startswith("exit=0\ndone")
        assert "prompt cache" in out
        assert "background" in out

    def test_cache_expiry_note_is_absent_for_fast_or_unbound_commands(self, tmp_path: Path) -> None:
        cfg = LaneBToolConfig(fs_root=tmp_path, cache_lifetime_seconds=0.05)
        with patch.object(shell_module, "monotonic", side_effect=[0.0, 0.04]):
            assert _shell(cfg)("printf done") == "exit=0\ndone"
        assert _shell(LaneBToolConfig(fs_root=tmp_path))("printf done") == "exit=0\ndone"

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

    def test_output_beyond_32_kib_is_capped_head_and_tail_with_a_marker(self, tmp_path: Path) -> None:
        cap = 32 * 1024
        exit_line, head, marker, tail = _shell(LaneBToolConfig(fs_root=tmp_path, shell_max_output_bytes=cap))(
            _FORTY_KB
        ).split("\n")
        assert exit_line == "exit=0"
        assert set(head) == {"a"}
        assert set(tail) == {"b"}
        assert "32 KiB Bash output cap" in marker
        assert "Read" in marker
        assert len(f"{exit_line}\n{head}{tail}".encode()) == cap

    def test_output_beyond_the_cap_is_kept_whole_in_the_file_the_marker_names(self, tmp_path: Path) -> None:
        marker = _shell(LaneBToolConfig(fs_root=tmp_path))(_FORTY_KB).split("\n")[2]
        (spilled,) = (tmp_path / ".t3-cache" / "tool-output").glob("*.log")
        assert spilled.read_bytes() == b"exit=0\n" + b"a" * 20000 + b"b" * 20000
        named = str(spilled.relative_to(tmp_path))
        assert named in marker
        assert "of 40007 bytes" in marker
        read = build_filesystem_toolset(tmp_path).tools["Read"].function
        assert read(named) == "exit=0\n" + "a" * 20000 + "b" * 20000

    def test_separate_keepers_spilling_in_the_same_second_get_distinct_complete_files(self, tmp_path: Path) -> None:
        class FrozenDatetime:
            @staticmethod
            def now(_timezone: object) -> datetime:
                return datetime(2026, 9, 21, tzinfo=UTC)

        first = b"a" * (32 * 1024 + 1)
        second = b"b" * (32 * 1024 + 1)
        with patch.object(shell_module, "datetime", FrozenDatetime):
            _OutputKeeper(tmp_path, _THIRTY_TWO_KIB).cap(first.decode())
            _OutputKeeper(tmp_path, _THIRTY_TWO_KIB).cap(second.decode())

        spills = list((tmp_path / ".t3-cache" / "tool-output").glob("*.log"))
        assert len(spills) == 2
        assert {spill.read_bytes() for spill in spills} == {first, second}

    def test_the_kept_output_never_shows_up_as_untracked_work(self, tmp_path: Path) -> None:
        worktree = tmp_path / "worktree"
        worktree.mkdir()
        subprocess.run([_GIT, "init", "-q", "-b", "main"], cwd=worktree, check=True)
        _shell(LaneBToolConfig(fs_root=worktree))(_FORTY_KB)
        status = subprocess.run(
            [_GIT, "status", "--porcelain"], cwd=worktree, capture_output=True, text=True, check=True
        )
        assert status.stdout == ""

    def test_a_spill_save_error_keeps_the_output_capped_and_explains_why(self, tmp_path: Path) -> None:
        with patch.object(_OutputKeeper, "_keep", side_effect=OSError("spill disk unavailable")):
            out = _shell(LaneBToolConfig(fs_root=tmp_path))(_FORTY_KB)

        assert out.startswith("exit=0\n" + "a" * 100)
        assert out.endswith("b" * 100)
        assert len(out.encode()) < len(("exit=0\n" + "a" * 20000 + "b" * 20000).encode())
        assert "could NOT be saved" in out
        assert "spill disk unavailable" in out

    def test_output_within_the_cap_is_returned_verbatim(self, tmp_path: Path) -> None:
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_max_output_bytes=_THIRTY_TWO_KIB)
        out = _shell(cfg)("head -c 30000 /dev/zero | tr '\\0' a")
        assert out == "exit=0\n" + "a" * 30000
        assert not (tmp_path / ".t3-cache").exists()

    def test_the_cap_never_splits_a_multibyte_character(self, tmp_path: Path) -> None:
        out = _shell(LaneBToolConfig(fs_root=tmp_path))("for i in $(seq 1 4000); do printf 'éééééééééé'; done")
        assert "bytes not shown" in out
        assert "\N{REPLACEMENT CHARACTER}" not in out

    def test_resolves_an_absolute_shell_path(self) -> None:
        # AH-11: the shell is resolved to an absolute path (not the bare "bash" name),
        # so the runner does not assume bash sits first on PATH.
        resolved = _resolve_shell()
        assert Path(resolved).name in {"bash", "sh"}
        # On any dev/CI host at least one POSIX shell is installed, so which() resolves
        # an absolute path; the bare-name fallback only fires when neither is present.
        assert Path(resolved).is_absolute()


class TestAStoppedCommandDiesWithEverythingItStarted:
    _FORKING = GATED_FORKING_COMMAND

    @staticmethod
    def _assert_group_stopped(directory: Path, pids: list[int]) -> None:
        try:
            assert all(is_gone_within(pid, seconds=3) for pid in pids)
            release_gate(directory)
            time.sleep(0.5)
            assert not (directory / "finished").exists()
        finally:
            for pid in pids:
                with suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)

    def test_a_cancelled_command_is_killed_and_the_cancellation_propagates(self, tmp_path: Path) -> None:
        async def run() -> list[int]:
            call = asyncio.create_task(_bash(LaneBToolConfig(fs_root=tmp_path))(self._FORKING))
            pids = await written_pids(tmp_path, "leader", "child")
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            return pids

        self._assert_group_stopped(tmp_path, asyncio.run(run()))

    def test_a_command_cancelled_while_it_is_still_being_spawned_is_killed_with_everything_it_started(
        self, tmp_path: Path
    ) -> None:
        async def run() -> tuple[list[int], asyncio.Task[str]]:
            with spawn_held_at_its_pipes() as released:
                call = asyncio.create_task(_bash(LaneBToolConfig(fs_root=tmp_path))(self._FORKING))
                pids = await written_pids(tmp_path, "leader", "child")
                call.cancel()
                await asyncio.sleep(0)
                released.set()
                await asyncio.wait({call}, timeout=10)
            return pids, call

        pids, call = asyncio.run(run())

        self._assert_group_stopped(tmp_path, pids)
        assert call.cancelled()

    def test_a_timed_out_command_is_killed_and_the_model_gets_the_retryable_error(self, tmp_path: Path) -> None:
        async def run() -> list[int]:
            call = asyncio.create_task(
                _bash(LaneBToolConfig(fs_root=tmp_path, shell_timeout_seconds=0.4))(self._FORKING)
            )
            with pytest.raises(ShellTimeoutError, match=r"timed out after 0\.4s"):
                await call
            return await written_pids(tmp_path, "leader", "child")

        self._assert_group_stopped(tmp_path, asyncio.run(run()))

    def test_a_descendant_outside_the_session_holding_the_pipes_does_not_delay_the_stop(self, tmp_path: Path) -> None:
        async def run() -> tuple[float, bool]:
            started = time.monotonic()
            with pytest.raises(ShellTimeoutError):
                await _bash(LaneBToolConfig(fs_root=tmp_path, shell_timeout_seconds=2))(
                    HOLDS_THE_PIPES_FROM_ANOTHER_SESSION
                )
            elapsed = time.monotonic() - started
            (escapee,) = await written_pids(tmp_path, "escapee")
            survived = is_running(escapee)
            with suppress(ProcessLookupError):
                os.kill(escapee, signal.SIGKILL)
            await asyncio.to_thread(is_gone_within, escapee, seconds=3)
            await asyncio.sleep(0.2)  # the pipes' EOF closes their transports before the loop ends
            return elapsed, survived

        elapsed, survived = asyncio.run(run())

        assert survived
        assert elapsed < 2 + 1.5


class TestTheCommandNeverReadsTheHostsInput:
    def test_a_command_reading_stdin_sees_its_end_even_when_the_host_holds_a_pipe_open(self, tmp_path: Path) -> None:
        reader, writer = os.pipe()
        saved = os.dup(0)
        os.dup2(reader, 0)
        try:
            out = _shell(LaneBToolConfig(fs_root=tmp_path, shell_timeout_seconds=3))("cat; echo eof")
        finally:
            os.dup2(saved, 0)
            for descriptor in (reader, writer, saved):
                os.close(descriptor)
        assert out == "exit=0\neof\n"


class TestTheReportReadsLikeText:
    @pytest.mark.parametrize(
        ("printed", "reported"),
        [("a\\r\\nb\\rc", "exit=0\na\nb\nc"), ("\\377", "exit=0\n\N{REPLACEMENT CHARACTER}")],
        ids=["carriage-returns-become-newlines", "a-non-utf8-byte-is-replaced"],
    )
    def test_the_report_reads_like_text_whatever_the_command_printed(
        self, tmp_path: Path, printed: str, reported: str
    ) -> None:
        assert _shell(LaneBToolConfig(fs_root=tmp_path))(f"printf '{printed}'") == reported


class TestOversizedOutputIsCapped:
    """The per-request cost of one huge return, bounded head+tail (#4816)."""

    _HUGE = 200_000

    def _huge_output(self, tmp_path: Path, cap: int) -> str:
        cfg = LaneBToolConfig(fs_root=tmp_path, shell_max_output_bytes=cap)
        return _shell(cfg)(f"python3 -c \"print('x' * {self._HUGE})\"")

    def test_a_huge_return_is_bounded_by_the_cap(self, tmp_path: Path) -> None:
        out = self._huge_output(tmp_path, 16 * 1024)
        (marker,) = (line for line in out.split("\n") if "bytes not shown" in line)
        assert len(out.encode()) - len(marker.encode()) - 2 <= 16 * 1024
        assert len(out) < self._HUGE

    def test_the_default_cap_bounds_a_huge_return(self, tmp_path: Path) -> None:
        out = _shell(LaneBToolConfig(fs_root=tmp_path))(f"python3 -c \"print('x' * {self._HUGE})\"")
        assert len(out) < self._HUGE

    def test_the_elision_names_the_dropped_bytes_and_the_recovery(self, tmp_path: Path) -> None:
        out = self._huge_output(tmp_path, 16 * 1024)
        assert "bytes not shown" in out
        assert "Read it with offset/limit" in out

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
