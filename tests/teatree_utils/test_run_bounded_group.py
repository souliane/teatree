"""``run_bounded_group`` — a deadline that terminates the whole process GROUP.

The failure these pin: ``subprocess.run(timeout=...)`` kills only the DIRECT child, so a
grandchild holding the captured pipe survives the deadline and keeps the parent's own
``communicate()`` waiting on an EOF that never comes. ``pass show`` is exactly that shape
(a bash script that execs ``gpg``), and 322 orphaned ``pass``/``gpg`` pairs outlived their
20s deadline by ten hours on the factory box.
"""

import contextlib
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from teatree.utils.run import CommandFailedError, TimeoutExpired, run_bounded_group

ESCAPEE_HOLD_SECONDS = 10


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return not _is_zombie(pid)


def _is_zombie(pid: int) -> bool:
    # A killed orphan in a container whose PID 1 never reaps stays a zombie, which kill(0) still finds.
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except OSError:
        return False
    return stat.rsplit(")", 1)[-1].split()[0] == "Z"


def _escapee_command(tmp_path: Path) -> tuple[list[str], Path]:
    """A command whose grandchild LEAVES the process group still holding the inherited pipes.

    ``fork`` then ``setsid``: a freshly forked child is never a group leader, so the escape
    is guaranteed rather than left to the shell's job-control mode.
    """
    pid_file = tmp_path / "escapee.pid"
    script = tmp_path / "escapee.py"
    script.write_text(
        "import os, time\n"
        "if os.fork() == 0:\n"
        "    os.setsid()\n"
        f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        f"    time.sleep({ESCAPEE_HOLD_SECONDS})\n"
        "    os._exit(0)\n"
        f"time.sleep({ESCAPEE_HOLD_SECONDS})\n"
    )
    return [sys.executable, str(script)], pid_file


def _reap(pid_file: Path) -> None:
    if pid_file.is_file():
        with contextlib.suppress(OSError, ValueError):
            os.kill(int(pid_file.read_text(encoding="utf-8")), signal.SIGKILL)


class TestBoundedGroupReturnsNormally:
    """The deadline is invisible to a command that finishes inside it."""

    def test_returns_stdout_on_success(self) -> None:
        assert run_bounded_group(["sh", "-c", "printf hello"], timeout=10).stdout == "hello"

    def test_raises_command_failed_on_nonzero(self) -> None:
        with pytest.raises(CommandFailedError) as err:
            run_bounded_group(["sh", "-c", "printf oops >&2; exit 3"], timeout=10)
        assert err.value.returncode == 3
        assert "oops" in err.value.stderr

    def test_expected_codes_none_returns_a_nonzero_result(self) -> None:
        result = run_bounded_group(
            ["sh", "-c", "printf refused >&2; exit 3"],
            timeout=10,
            expected_codes=None,
        )

        assert result.returncode == 3
        assert result.stderr == "refused"


class TestBoundedGroupKillsTheWholeGroup:
    """A grandchild that outlives its parent is reaped WITH the group, not orphaned."""

    def test_deadline_is_honoured_when_a_grandchild_holds_the_pipe(self, tmp_path: Path) -> None:
        """The `pass`-shaped case: the child exits, a grandchild keeps the captured pipe open.

        With a direct-child-only kill the read blocks forever on ``communicate()``; the
        deadline must fire regardless of who holds the descriptor.
        """
        started = time.monotonic()
        with pytest.raises(TimeoutExpired):
            run_bounded_group(["sh", "-c", f"sleep 30 & echo $! > {tmp_path / 'gc.pid'}; sleep 30"], timeout=2)
        assert time.monotonic() - started < 20, "the deadline did not bound the read"

    def test_no_descendant_survives_the_deadline(self, tmp_path: Path) -> None:
        """The orphan-accumulation bug: a killed read must leave nothing behind."""
        pid_file = tmp_path / "gc.pid"
        with pytest.raises(TimeoutExpired):
            run_bounded_group(["sh", "-c", f"sleep 30 & echo $! > {pid_file}; wait"], timeout=2)
        grandchild = int(pid_file.read_text().strip())
        deadline = time.monotonic() + 5
        while _alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(grandchild), (
            f"grandchild {grandchild} outlived the deadline — orphaned, exactly as on the box"
        )

    def test_the_child_leads_its_own_process_group(self) -> None:
        """The kill boundary only exists because the child is a session leader."""
        result = run_bounded_group(["sh", "-c", "ps -o pgid= -p $$"], timeout=10)
        assert int(result.stdout.strip()) != os.getpgid(os.getpid())


class TestBoundedGroupBoundsTheDrainAfterTheKill:
    """A descendant the group kill cannot reach still holds the pipes — the deadline holds anyway."""

    def test_an_escaped_descendant_holding_the_pipes_does_not_extend_the_deadline(self, tmp_path: Path) -> None:
        cmd, pid_file = _escapee_command(tmp_path)
        started = time.monotonic()
        try:
            with pytest.raises(TimeoutExpired):
                run_bounded_group(cmd, timeout=2)
            elapsed = time.monotonic() - started
            assert elapsed < ESCAPEE_HOLD_SECONDS / 2, (
                f"the post-kill drain ran {elapsed:.1f}s past a 2s deadline — it waits on an EOF "
                "the escaped descendant owns, which run_checked's POSIX path never does"
            )
            assert pid_file.is_file(), "the escapee never started — nothing held the pipes open"
            assert _alive(int(pid_file.read_text(encoding="utf-8"))), "the escapee died with the group — vacuous"
        finally:
            _reap(pid_file)


class TestBoundedGroupSignalSafety:
    """Killing the group must never reach the caller's own group (pgid 0 / -1)."""

    def test_the_kill_never_targets_the_callers_own_group(self, monkeypatch: pytest.MonkeyPatch) -> None:
        targeted: list[int] = []
        real_killpg = os.killpg

        def record(pgid: int, sig: int) -> None:
            targeted.append(pgid)
            real_killpg(pgid, sig)

        monkeypatch.setattr(os, "killpg", record)
        with pytest.raises(TimeoutExpired):
            run_bounded_group(["sh", "-c", "sleep 30"], timeout=1)

        assert targeted, "the deadline never reached the group kill"
        assert os.getpgid(os.getpid()) not in targeted, f"the caller's own group was signalled: {targeted}"
        assert all(pgid > 1 for pgid in targeted), f"pgid 0/-1 signals every process the caller may: {targeted}"
