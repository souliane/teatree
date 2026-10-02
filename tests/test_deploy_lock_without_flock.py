# test-path: cross-cutting — drives deploy/deploy.sh (no src mirror).
"""deploy.sh's mkdir lock, on a host that ships no flock.

The single-convergence invariant is the whole point of the lock: two overlapping
runs each set `worker_quiescing` ON, and the older one re-asserts it after the
newer run's init cleared it, stranding admission OFF until someone notices. macOS
has no flock, so that host falls back to a `mkdir` lock — and a lock that hands the
same convergence slot to two runs is the same outage the flock exists to prevent.

These run the SHIPPED bytes: each harness splices the lock block out of deploy.sh
between anchors and executes it under `tmp_path`, on a PATH that genuinely holds no
flock, so the fallback branch is the one under test on Linux CI too.
"""

import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_DEPLOY_SH = _ROOT / "deploy" / "deploy.sh"
_BASH = shutil.which("bash") or "/bin/bash"

#: Anchors bounding the acquisition + release + trap install, so every probe below runs
#: the shipped code rather than a re-typed copy of it.
_LOCK_START = 'DEPLOY_LOCK="${TEATREE_DEPLOY_LOCK:-/tmp/teatree-deploy.lock}"'
_FAIL_SAFE_START = "_DRAINED=false"
_LOCK_END = "trap '_clear_quiescing_if_stranded; _release_deploy_record' EXIT"

#: Everything the lock block shells out to. flock is deliberately absent.
#: `ps` is the one optional member — absent it, the lock degrades to `kill -0` alone,
#: which is what the EPERM probes below skip on.
_LOCK_BLOCK_TOOLS = ("mkdir", "cat", "find", "rm", "sleep")

_HOLD_SECONDS = 2
_RUN_TIMEOUT = 20


def _shipped(start: str, end: str = _LOCK_END) -> str:
    body = _DEPLOY_SH.read_text(encoding="utf-8")
    first, last = body.find(start), body.find(end)
    moved = f"deploy.sh's {start!r}..{end!r} block moved — re-anchor this probe"
    assert first != -1, moved
    assert last > first, moved
    return body[first : last + len(end)] + "\n"


def _write_stub(path: Path, exit_code: int) -> None:
    """A stub reached through the restricted PATH, so its interpreter must be absolute."""
    path.unlink(missing_ok=True)
    path.write_text(f"#!{_BASH}\nexit {exit_code}\n", encoding="utf-8")
    path.chmod(0o755)


def _shipped_int(name: str) -> int:
    body = _DEPLOY_SH.read_text(encoding="utf-8")
    match = re.search(rf"^{name}=(\d+)$", body, re.MULTILINE)
    assert match, f"deploy.sh no longer sets {name} to a literal — re-anchor this probe"
    return int(match.group(1))


def _shipped_function(name: str) -> str:
    body = _DEPLOY_SH.read_text(encoding="utf-8")
    start = body.find(f"{name}() {{")
    assert start != -1, f"deploy.sh no longer defines {name}() — re-anchor this probe"
    end = body.find("\n}\n", start)
    assert end > start, f"{name}() is no longer brace-terminated — re-anchor this probe"
    return body[start : end + len("\n}\n")]


@pytest.fixture
def flockless_path(tmp_path: Path) -> Path:
    """A PATH carrying every tool the lock block needs EXCEPT flock — the macOS host."""
    bin_dir = tmp_path / "flockless-bin"
    bin_dir.mkdir()
    for tool in (*_LOCK_BLOCK_TOOLS, "ps"):
        real = shutil.which(tool)
        assert real or tool == "ps", f"{tool} is not on this host's PATH"
        if real:
            (bin_dir / tool).symlink_to(real)
    assert shutil.which("flock", path=str(bin_dir)) is None, "the fixture must hide flock"
    return bin_dir


@pytest.fixture
def lock_path(tmp_path: Path) -> Path:
    """The lock file deploy.sh derives its lock DIRECTORY from (`<lock>.d`)."""
    holder = tmp_path / "locks"
    holder.mkdir()
    return holder / "teatree-deploy.lock"


def _write_acquirer(tmp_path: Path, *, prelude: str = "", hold_seconds: int = 0) -> Path:
    script = tmp_path / "acquire.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\nCOMPOSE_FILE=/dev/null\n"
        f"{prelude}"
        f"{_shipped(_LOCK_START)}"
        'echo "CONVERGED"\n'
        f"sleep {hold_seconds}\n",
        encoding="utf-8",
    )
    return script


def _acquire_env(flockless_path: Path, lock_path: Path) -> dict[str, str]:
    return {"PATH": str(flockless_path), "TEATREE_DEPLOY_LOCK": str(lock_path)}


def _run_acquirer(script: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [_BASH, str(script)],
        check=False,
        capture_output=True,
        encoding="utf-8",
        env=env,
        timeout=_RUN_TIMEOUT,
    )


def _seed_lock(lock_path: Path, *, pid: str | None) -> Path:
    lock_dir = Path(f"{lock_path}.d")
    lock_dir.mkdir()
    if pid is not None:
        (lock_dir / "pid").write_text(pid, encoding="utf-8")
    return lock_dir


def _age(path: Path, *, minutes: int) -> None:
    stale = path.stat().st_mtime - minutes * 60
    os.utime(path, (stale, stale))


@pytest.fixture
def live_pid() -> Iterator[int]:
    proc = subprocess.Popen([shutil.which("sleep") or "/bin/sleep", "30"])
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


@pytest.fixture
def reaped_pid() -> int:
    """A pid that has exited AND been reaped, so it names no process."""
    proc = subprocess.Popen([shutil.which("sleep") or "/bin/sleep", "0"])
    proc.wait()
    return proc.pid


class TestAcquisitionIsAtomic:
    @pytest.mark.parametrize(
        ("pid_bytes", "state"),
        [
            (None, "no pid file yet"),
            ("", "an empty pid file"),
            ("   \n", "a whitespace-only pid file"),
            ("not-a-pid\n", "a half-written pid file"),
        ],
    )
    def test_a_lock_naming_no_readable_pid_is_held_not_free(
        self, tmp_path: Path, flockless_path: Path, lock_path: Path, pid_bytes: str | None, state: str
    ) -> None:
        # `mkdir` publishes the lock one syscall before the pid lands in it. A second
        # deploy arriving in that window read an empty pid as "free", reclaimed the
        # winner's directory and converged alongside it — two convergences, the exact
        # outage the lock exists to prevent, needing no dead lock and no pid reuse.
        lock_dir = _seed_lock(lock_path, pid=pid_bytes)

        result = _run_acquirer(_write_acquirer(tmp_path), _acquire_env(flockless_path, lock_path))

        assert result.returncode == 0, result.stderr
        assert "already holds" in result.stderr, f"{state} must read as HELD: {result.stderr}"
        assert "reclaiming" not in result.stderr
        assert "CONVERGED" not in result.stdout
        assert lock_dir.is_dir(), "the winner's lock directory was destroyed"

    def test_simultaneous_acquirers_produce_exactly_one_convergence(
        self, tmp_path: Path, flockless_path: Path, lock_path: Path
    ) -> None:
        go = tmp_path / "go"
        script = _write_acquirer(
            tmp_path,
            prelude=f'while [ ! -e "{go}" ]; do :; done\n',
            hold_seconds=_HOLD_SECONDS,
        )
        env = _acquire_env(flockless_path, lock_path)

        racers = [
            subprocess.Popen(
                [_BASH, str(script)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                encoding="utf-8",
                env=env,
            )
            for _ in range(8)
        ]
        try:
            go.touch()
            outputs = [proc.communicate(timeout=_RUN_TIMEOUT) for proc in racers]
        finally:
            for proc in racers:
                proc.kill()

        converged = [out for out, _ in outputs if "CONVERGED" in out]
        assert len(converged) == 1, f"{len(converged)} of 8 acquirers converged at once"


class TestLivenessIsNotKillMinusZeroAlone:
    #: `kill -0` fails with EPERM on another user's process, and the default lock lives in
    #: world-shared /tmp. `enable -n kill` drops bash's builtin so this stub stands in for
    #: that refusal without needing a second user account on the host.
    _REFUSING_KILL = "enable -n kill\n"

    @pytest.fixture
    def refusing_kill_path(self, flockless_path: Path) -> Path:
        _write_stub(flockless_path / "kill", 1)
        return flockless_path

    def test_a_live_pid_whose_signal_is_refused_reads_alive(
        self, tmp_path: Path, refusing_kill_path: Path, lock_path: Path, live_pid: int
    ) -> None:
        if not (refusing_kill_path / "ps").exists():
            pytest.skip("no ps on this host — the lock degrades to `kill -0` alone")
        lock_dir = _seed_lock(lock_path, pid=f"{live_pid}\n")

        result = _run_acquirer(
            _write_acquirer(tmp_path, prelude=self._REFUSING_KILL),
            _acquire_env(refusing_kill_path, lock_path),
        )

        assert result.returncode == 0, result.stderr
        assert "already holds" in result.stderr, result.stderr
        assert "CONVERGED" not in result.stdout
        assert lock_dir.is_dir(), "another user's live convergence was reclaimed"

    def test_a_reaped_pid_still_reads_dead_under_the_same_refusal(
        self, tmp_path: Path, refusing_kill_path: Path, lock_path: Path, reaped_pid: int
    ) -> None:
        # The control: without it, "held" above could just as well mean the probe
        # never distinguishes anything.
        _seed_lock(lock_path, pid=f"{reaped_pid}\n")

        result = _run_acquirer(
            _write_acquirer(tmp_path, prelude=self._REFUSING_KILL),
            _acquire_env(refusing_kill_path, lock_path),
        )

        assert result.returncode == 0, result.stderr
        assert "reclaiming" in result.stderr, result.stderr
        assert "CONVERGED" in result.stdout


class TestAgeBoundsTheLock:
    def test_a_lock_older_than_any_convergence_is_reclaimed_from_a_live_pid(
        self, tmp_path: Path, flockless_path: Path, lock_path: Path, live_pid: int
    ) -> None:
        # A recycled pid reads alive forever, so a pid check alone would wedge every
        # later deploy into exiting 0 having converged nothing — the very failure this
        # branch exists to fix, reached by another door.
        lock_dir = _seed_lock(lock_path, pid=f"{live_pid}\n")
        _age(lock_dir, minutes=24 * 60)

        result = _run_acquirer(_write_acquirer(tmp_path), _acquire_env(flockless_path, lock_path))

        assert result.returncode == 0, result.stderr
        assert "has held it over" in result.stderr, result.stderr
        assert "CONVERGED" in result.stdout

    def test_a_fresh_lock_on_the_same_live_pid_is_held(
        self, tmp_path: Path, flockless_path: Path, lock_path: Path, live_pid: int
    ) -> None:
        _seed_lock(lock_path, pid=f"{live_pid}\n")

        result = _run_acquirer(_write_acquirer(tmp_path), _acquire_env(flockless_path, lock_path))

        assert result.returncode == 0, result.stderr
        assert "already holds" in result.stderr, result.stderr
        assert "CONVERGED" not in result.stdout


class TestTheReclaimLoopIsBoundedAndDiagnosed:
    def test_a_reclaim_that_cannot_remove_the_lock_names_the_deploy_step(
        self, tmp_path: Path, flockless_path: Path, lock_path: Path, reaped_pid: int
    ) -> None:
        # The unchecked `rm -rf` did end the run — `set -e` saw to that — but only a bare
        # `rm: … Permission denied` reached the Action log, naming neither deploy.sh nor
        # the lock.
        _seed_lock(lock_path, pid=f"{reaped_pid}\n")
        _write_stub(flockless_path / "rm", 1)

        result = _run_acquirer(_write_acquirer(tmp_path), _acquire_env(flockless_path, lock_path))

        assert result.returncode == 1
        assert "cannot remove the stale" in result.stderr, result.stderr
        assert "CONVERGED" not in result.stdout

    def test_a_lock_that_keeps_reappearing_gives_up_after_a_bounded_wait(
        self, tmp_path: Path, flockless_path: Path, lock_path: Path, reaped_pid: int
    ) -> None:
        # A racer recreating the lock as fast as it is reclaimed. Stubbing the two calls
        # the loop turns on is what makes that deterministic: `mkdir` never wins the
        # slot, `rm` reports the reclaim it did not do.
        _seed_lock(lock_path, pid=f"{reaped_pid}\n")
        for tool, exit_code in (("mkdir", 1), ("rm", 0)):
            _write_stub(flockless_path / tool, exit_code)

        started = time.monotonic()
        result = _run_acquirer(_write_acquirer(tmp_path), _acquire_env(flockless_path, lock_path))
        elapsed = time.monotonic() - started

        reclaims = _shipped_int("DEPLOY_LOCK_MAX_RECLAIMS")
        assert result.returncode == 1
        assert "keeps reappearing" in result.stderr, result.stderr
        assert result.stderr.count("reclaiming") == reclaims
        assert elapsed >= reclaims, "the loop retried without sleeping between attempts"


class TestReleaseIsStructurallyScopedToOurOwnLock:
    """The trap's other job is unrelated to the lock, so hoisting it must stay safe."""

    def _write_releaser(self, tmp_path: Path, lock_dir: Path, *, claim: bool) -> Path:
        script = tmp_path / "release.sh"
        script.write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\nCOMPOSE_FILE=/dev/null\n"
            f'DEPLOY_LOCK_DIR="{lock_dir}"\n'
            + (f'printf "%s\\n" "$$" >"{lock_dir}/pid"\n' if claim else "")
            + _shipped_function("_release_deploy_lock")
            + _shipped(_FAIL_SAFE_START),
            encoding="utf-8",
        )
        return script

    def test_a_lock_this_process_never_acquired_survives_the_trap(
        self, tmp_path: Path, flockless_path: Path, lock_path: Path, live_pid: int
    ) -> None:
        lock_dir = _seed_lock(lock_path, pid=f"{live_pid}\n")

        result = _run_acquirer(
            self._write_releaser(tmp_path, lock_dir, claim=False),
            {"PATH": str(flockless_path)},
        )

        assert result.returncode == 0, result.stderr
        assert lock_dir.is_dir(), "the trap deleted another convergence's lock"

    def test_a_lock_this_process_is_named_in_is_released(
        self, tmp_path: Path, flockless_path: Path, lock_path: Path
    ) -> None:
        lock_dir = _seed_lock(lock_path, pid=None)

        result = _run_acquirer(
            self._write_releaser(tmp_path, lock_dir, claim=True),
            {"PATH": str(flockless_path)},
        )

        assert result.returncode == 0, result.stderr
        assert not lock_dir.exists(), "our own lock was left behind"
