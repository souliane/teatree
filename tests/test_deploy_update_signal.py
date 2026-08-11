# test-path: cross-cutting — drives deploy/t3 (no src mirror).
"""An update must not look like an outage from the host CLI (#4214).

A convergence recreates the control-DB services, and a ``t3`` call landing in that
window used to fail with docker's own ``service "teatree-admin" is not running`` —
byte-identical to a genuine outage, so a monitor could neither wait it out nor
escalate on it. Two changes make the two distinguishable: the wrapper dispatches
into whichever control-DB service IS running, and when none is, it reads
``deploy.sh``'s convergence flock and says so instead of failing opaquely.

Runs the REAL ``deploy/t3`` with a stub ``docker`` and a REAL ``flock`` held on a
tmp lock file, so the probe under test is the shipped one.
"""

import os
import shutil
import stat
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

_WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "t3"
_FLOCK = shutil.which("flock")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or _FLOCK is None or not Path("/proc/locks").exists(),
    reason="needs bash + flock + Linux /proc/locks (present in the deploy image and CI)",
)

_UPDATE_IN_PROGRESS_EXIT = 75
_DISPATCHED_ONE_OFF = "DISPATCHED-RUN"

# `STUB_RUNNING_SERVICES` is the space-separated set the stub reports as running, so a
# test can model any stage of a staged swap. `compose ps --status running --quiet
# <svc>` answers with an id only for a member of that set.
#
# `STUB_RUNNING_AT` (epoch seconds) additionally withholds every answer until that
# instant, modelling a route that is genuinely absent for the first seconds of a swap
# and comes back mid-wait. A wall-clock gate rather than a probe counter: the wrapper
# probes each candidate service in turn, so a count is an artifact of the candidate
# list while elapsed time is what the loop actually waits on.
_DOCKER_STUB = f"""#!/usr/bin/env bash
case "$1" in
version) exit "${{STUB_DAEMON_EXIT:-0}}" ;;
image) exit "${{STUB_IMAGE_EXIT:-0}}" ;;
esac
shift
while [ "${{1:-}}" = -f ] || [ "${{1:-}}" = -p ]; do shift 2; done
sub="${{1:-}}"
shift || true
case "$sub" in
ps)
    svc="${{@: -1}}"
    if [ -n "${{STUB_RUNNING_AT:-}}" ] && [ "$(date +%s)" -lt "${{STUB_RUNNING_AT}}" ]; then
        exit 0
    fi
    case " ${{STUB_RUNNING_SERVICES:-}} " in
    *" $svc "*) printf '%s\\n' "cid-$svc" ;;
    esac
    exit 0
    ;;
config) printf '%s\\n' "teatree-worker:local" ; exit 0 ;;
exec)
    while :; do
        case "${{1:-}}" in
        -T) shift ;;
        --env) shift 2 ;;
        *) break ;;
        esac
    done
    printf 'DISPATCHED-EXEC %s\\n' "${{1:-}}"
    exit 0
    ;;
run) printf '{_DISPATCHED_ONE_OFF}\\n' ; exit 0 ;;
esac
exit 0
"""


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def wrapper(tmp_path: Path) -> Path:
    """The real wrapper, copied out so the checkout it runs from is ours."""
    deploy = tmp_path / "checkout" / "deploy"
    deploy.mkdir(parents=True)
    entry = deploy / "t3"
    shutil.copy2(_WRAPPER, entry)
    entry.chmod(entry.stat().st_mode | stat.S_IXUSR)
    return entry


@contextmanager
def _held_lock(path: Path) -> Iterator[None]:
    """Hold a REAL flock on *path* for the body, the way a live deploy.sh does."""
    path.touch()
    assert _FLOCK is not None
    holder = subprocess.Popen([_FLOCK, str(path), "-c", "sleep 60"])
    try:
        deadline = 400
        while deadline and not _lock_visible(path):
            deadline -= 1
        assert _lock_visible(path), "the test's own flock never appeared in /proc/locks"
        yield
    finally:
        holder.kill()
        holder.wait()


def _lock_visible(path: Path) -> bool:
    st = path.stat()
    needle = f" {os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino} "
    return needle in Path("/proc/locks").read_text(encoding="utf-8")


def _run(wrapper: Path, tmp_path: Path, **env_extra: str) -> subprocess.CompletedProcess[str]:
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(exist_ok=True)
    _write_exec(stub_bin / "docker", _DOCKER_STUB)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(exist_ok=True)

    env = {k: v for k, v in os.environ.items() if k not in {"TEATREE_SOURCE_MOUNT", "TEATREE_INVOCATION_CWD"}}
    env["PATH"] = f"{stub_bin}{os.pathsep}{env['PATH']}"
    env["TEATREE_HOST_HOME"] = str(home)
    # Short-circuits the host `glab` resolution — irrelevant here and not always present.
    env["GITLAB_TOKEN"] = "unused"
    env["TEATREE_DEPLOY_LOCK"] = str(tmp_path / "deploy.lock")
    env.update(env_extra)

    return subprocess.run(
        [str(wrapper), "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=elsewhere,
        check=False,
    )


class TestASiblingRouteIsUsedWhileTheOtherIsSwapped:
    def test_the_admin_serves_the_call_while_the_worker_is_being_recreated(self, wrapper: Path, tmp_path: Path) -> None:
        proc = _run(wrapper, tmp_path, STUB_RUNNING_SERVICES="teatree-admin")

        assert proc.returncode == 0
        assert "DISPATCHED-EXEC teatree-admin" in proc.stdout
        assert _DISPATCHED_ONE_OFF not in proc.stdout, "a live sibling must be used, never a one-off container"

    def test_the_fallback_is_announced_so_the_operator_knows_which_container_ran(
        self, wrapper: Path, tmp_path: Path
    ) -> None:
        proc = _run(wrapper, tmp_path, STUB_RUNNING_SERVICES="teatree-admin")

        assert "teatree-admin" in proc.stderr
        assert "teatree-worker" in proc.stderr

    def test_the_preferred_service_still_wins_when_it_is_running(self, wrapper: Path, tmp_path: Path) -> None:
        proc = _run(wrapper, tmp_path, STUB_RUNNING_SERVICES="teatree-worker teatree-admin")

        assert "DISPATCHED-EXEC teatree-worker" in proc.stdout


class TestAnUpdateIsDistinguishableFromAnOutage:
    def test_a_convergence_in_flight_is_named_rather_than_reported_as_a_dead_service(
        self, wrapper: Path, tmp_path: Path
    ) -> None:
        with _held_lock(tmp_path / "deploy.lock"):
            proc = _run(wrapper, tmp_path, STUB_RUNNING_SERVICES="", TEATREE_UPDATE_WAIT_SECONDS="2")

        assert proc.returncode == _UPDATE_IN_PROGRESS_EXIT, (
            "an update needs its own exit code so a caller can branch on it instead of parsing text"
        )
        assert "update is in progress" in proc.stderr
        assert "is not running" not in proc.stdout

    def test_a_live_route_serves_the_call_without_ever_waiting(self, wrapper: Path, tmp_path: Path) -> None:
        # A convergence is in flight AND a route is up — the staged swap's normal shape.
        # The wait is for the gap between two stages, so a live route must skip it.
        with _held_lock(tmp_path / "deploy.lock"):
            proc = _run(
                wrapper,
                tmp_path,
                STUB_RUNNING_SERVICES="teatree-admin",
                TEATREE_UPDATE_WAIT_SECONDS="4",
            )

        assert proc.returncode == 0
        assert "DISPATCHED-EXEC teatree-admin" in proc.stdout
        assert "waiting up to" not in proc.stderr, "a route that is already up must not enter the wait at all"

    def test_a_route_that_returns_mid_wait_simply_serves_the_call(self, wrapper: Path, tmp_path: Path) -> None:
        # Nothing answers at first, so the wrapper enters the wait; the admin appears a
        # few seconds in, as it does when its stage of the swap completes. Only a
        # re-probe INSIDE the loop can see that, which is what this pins: without one
        # the loop sleeps out its whole budget and reports the update-in-progress exit
        # even though the substrate came back.
        with _held_lock(tmp_path / "deploy.lock"):
            proc = _run(
                wrapper,
                tmp_path,
                STUB_RUNNING_SERVICES="teatree-admin",
                STUB_RUNNING_AT=str(int(time.time()) + 3),
                TEATREE_UPDATE_WAIT_SECONDS="30",
            )

        assert proc.returncode == 0, f"the wrapper must recover mid-wait, not exit {proc.returncode}: {proc.stderr}"
        assert "waiting up to" in proc.stderr, "the wait must actually have been entered for this to prove anything"
        assert "DISPATCHED-EXEC teatree-admin" in proc.stdout


class TestTheStoppedStackPathIsUnchanged:
    def test_no_convergence_in_flight_still_dispatches_the_one_off_container(
        self, wrapper: Path, tmp_path: Path
    ) -> None:
        proc = _run(wrapper, tmp_path, STUB_RUNNING_SERVICES="")

        assert proc.returncode == 0
        assert _DISPATCHED_ONE_OFF in proc.stdout
        assert str(_UPDATE_IN_PROGRESS_EXIT) not in proc.stderr

    def test_an_unreachable_daemon_still_names_the_deploy_script(self, wrapper: Path, tmp_path: Path) -> None:
        proc = _run(wrapper, tmp_path, STUB_RUNNING_SERVICES="", STUB_DAEMON_EXIT="1")

        assert proc.returncode != 0
        assert _DISPATCHED_ONE_OFF not in proc.stdout
        assert "deploy/deploy.sh" in proc.stderr
