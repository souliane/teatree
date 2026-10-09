# test-path: cross-cutting — what deploy/deploy.sh hands the venues that judge whether it is still running.
"""A convergence proves it is alive by a heartbeat, and tells the containers what host they run on.

The in-progress record used to be stamped once at the start, so a legal drain longer
than the readers' guessed budget aged it out while ``deploy.sh`` was still running: the
watchdog paged it as an outage and the doctor cleared the quiescing gate mid-drain. The
doctor's fallback, ``/host-proc``, is the Docker VM's table on a macOS host, where a
``deploy.sh`` running on the Mac never appears.
"""

import os
import re
import shutil
import subprocess
import time
from pathlib import Path

import yaml

from teatree.cli.doctor import deploy_liveness

_DEPLOY_DIR = Path(__file__).resolve().parents[1] / "deploy"
_BASH = shutil.which("bash") or "/bin/bash"


def _shell_constant(script: str, name: str) -> int:
    match = re.search(rf"^{name}=(\d+)$", (_DEPLOY_DIR / script).read_text(encoding="utf-8"), re.MULTILINE)
    assert match, f"{script} declares no {name}"
    return int(match.group(1))


def _record(*, beat_age: int, deadline_in: int) -> str:
    now = int(time.time())
    return f"4242 {now - beat_age} {now + deadline_in}\n"


def _watchdog_marker_fresh(tmp_path: Path, record: str) -> bool:
    lock = tmp_path / "deploy.lock"
    lock.write_text(record, encoding="utf-8")
    harness = tmp_path / "harness.sh"
    harness.write_text(f'source "{_DEPLOY_DIR / "watchdog.sh"}"\ndeploy_marker_fresh\n', encoding="utf-8")
    env = {**os.environ, "TEATREE_WATCHDOG_DEPLOY_LOCK": str(lock)}
    return subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=env).returncode == 0


def test_every_reader_calls_a_record_stale_after_three_missed_beats() -> None:
    stale_after = 3 * _shell_constant("deploy.sh", "DEPLOY_HEARTBEAT_INTERVAL")

    assert _shell_constant("watchdog.sh", "DEPLOY_HEARTBEAT_STALE_AFTER") == stale_after
    assert stale_after == deploy_liveness.HEARTBEAT_STALE_AFTER_SECONDS


class TestTheWatchdogJudgesTheHeartbeat:
    def test_a_recent_beat_before_the_deadline_is_a_convergence(self, tmp_path: Path) -> None:
        assert _watchdog_marker_fresh(tmp_path, _record(beat_age=5, deadline_in=3600))

    def test_a_beat_older_than_three_intervals_is_not(self, tmp_path: Path) -> None:
        assert not _watchdog_marker_fresh(tmp_path, _record(beat_age=600, deadline_in=3600))

    def test_a_record_past_its_own_deadline_is_not(self, tmp_path: Path) -> None:
        assert not _watchdog_marker_fresh(tmp_path, _record(beat_age=5, deadline_in=-1))

    def test_a_cleared_record_is_not(self, tmp_path: Path) -> None:
        assert not _watchdog_marker_fresh(tmp_path, "")


def test_every_container_learns_the_host_os() -> None:
    compose = yaml.safe_load((_DEPLOY_DIR / "docker-compose.yml").read_text(encoding="utf-8"))

    for name, service in compose["services"].items():
        assert service["environment"].get("TEATREE_HOST_OS") == "${TEATREE_HOST_OS:-unknown}", name


def _heartbeat_harness(tmp_path: Path, *, kill_is_a_no_op: bool) -> str:
    """deploy.sh's heartbeat, its release and the record writers it sources, verbatim, beating every second.

    The lock is read 3s after release.
    """
    body = (_DEPLOY_DIR / "deploy.sh").read_text(encoding="utf-8")
    beat = body[body.index("DEPLOY_HEARTBEAT_INTERVAL=60\n") : body.index("_DEPLOY_HEARTBEAT_PID=$!") + 24]
    release = body[body.index("_release_deploy_record() {") :]
    release = release[: release.index("\n}\n") + 3]
    lock = tmp_path / "deploy.lock"
    return (
        "set -euo pipefail\n"
        f"DEPLOY_LOCK={lock}\nDEPLOY_LOCK_MAX_AGE_MINUTES=90\n"
        f". {_DEPLOY_DIR / 'deploy-lock.sh'}\n"
        + ("kill() { :; }\n" if kill_is_a_no_op else "")
        + beat.replace("DEPLOY_HEARTBEAT_INTERVAL=60", "DEPLOY_HEARTBEAT_INTERVAL=1")
        + "\n"
        + release
        + "sleep 1.5\n_release_deploy_record\nsleep 3\n"
        + 'if builtin kill -0 "$_DEPLOY_HEARTBEAT_PID" 2>/dev/null; then echo beat-alive; else echo beat-gone; fi\n'
        + 'printf "record=[%s]\\n" "$(cat "$DEPLOY_LOCK")"\n'
        + 'builtin kill "$_DEPLOY_HEARTBEAT_PID" 2>/dev/null || true\n'
    )


def _run_heartbeat(tmp_path: Path, *, kill_is_a_no_op: bool) -> str:
    harness = tmp_path / "heartbeat.sh"
    harness.write_text(_heartbeat_harness(tmp_path, kill_is_a_no_op=kill_is_a_no_op), encoding="utf-8")
    return subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=True, timeout=30).stdout


class TestReleasingTheRecordStopsTheBeat:
    def test_the_release_stops_the_heartbeat_loop(self, tmp_path: Path) -> None:
        out = _run_heartbeat(tmp_path, kill_is_a_no_op=False)

        assert "beat-gone" in out
        assert "record=[]" in out

    def test_a_beat_that_survives_the_release_never_rewrites_the_cleared_record(self, tmp_path: Path) -> None:
        out = _run_heartbeat(tmp_path, kill_is_a_no_op=True)

        assert "beat-alive" in out, "the control: the loop really outlived the release here"
        assert "record=[]" in out
