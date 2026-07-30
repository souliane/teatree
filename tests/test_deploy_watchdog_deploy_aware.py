# test-path: cross-cutting — drives deploy/watchdog.sh (no src mirror).
"""The watchdog's deploy-awareness (deploy/watchdog.sh, #3732).

A convergence fast-forwards the checkout and swaps the image, which RECREATES the
worker and the slack-listener. A pass landing inside that window samples a healthy
rolling deploy and used to DM it as an outage, so every merge produced a red DM
that needed no action. Three findings — and only those three — are gated on it:
worker-flock-not-held, slack-listener-down, clone-behind-origin.

Runs the REAL ``run_pass`` (the script is sourced, its dispatch guarded so it does
not auto-run) with a stub ``docker`` modelling the compose calls, and a REAL
``flock`` held on a tmp lock file to drive the deploy-in-flight probe.
"""

import contextlib
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

WATCHDOG = Path(__file__).resolve().parents[1] / "deploy" / "watchdog.sh"
_BASH = shutil.which("bash") or "bash"
_FLOCK = shutil.which("flock")

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("python3") is None or not Path("/proc/locks").exists(),
    reason="needs bash + python3 + Linux /proc/locks (present in the deploy image and CI)",
)

_FLOCK_FAIL = "no loop worker holds the flock — the reactive Slack-answer cycle never runs. Start `t3 worker ensure`."
_LISTENER_FAIL = "slack-listener receiver is DOWN — no inbound Slack event is being received."
_CLONE_FAIL = "teatree clone at <clone-path> is 7 commit(s) behind origin/main — run `t3 update`."
_OTHER_FAIL = "Compose service teatree-worker is exited"
_WEDGED_FAIL = "the worker holds the flock but these loops are not advancing their cadence: inbox, review"


def _created(*, seconds_ago: float = 0) -> str:
    """A container creation time in `docker inspect .Created`'s real shape (RFC3339Nano, UTC)."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - seconds_ago)) + ".683288764Z"


def _verdict(*messages: str) -> str:
    findings = ", ".join(f'{{"level": "FAIL", "message": "{m}"}}' for m in messages)
    return f'{{"ok": false, "findings": [{findings}]}}'


def _write_docker_stub(bin_dir: Path) -> None:
    """A ``docker`` shim modelling the docker + compose calls ``run_pass`` makes.

    The container-creation probe is TWO calls, exactly as the real one is: ``ps
    --format '{{.ID}}'`` then ``inspect --format '{{.Created}}'``. ``STUB_CREATED``
    supplies the RFC3339 creation time (empty → no containers). Every invocation is
    appended to ``STUB_DOCKER_LOG`` so the argv shape itself can be asserted.

    A doctor ``exec`` prints ``STUB_DOCTOR_JSON``; a ``notify send`` exec captures
    the piped DM body to ``STUB_NOTIFY_FILE``.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'printf "%s\\n" "$*" >>"${STUB_DOCKER_LOG:-/dev/null}"\n'
        'if [ "$1" != compose ]; then\n'
        '  case "$1 $*" in\n'
        '    "ps "*.ID*) [ -z "${STUB_CREATED:-}" ] || printf "%s\\n" "stubcid" ;;\n'
        '    "inspect "*) printf "%s\\n" "${STUB_CREATED:-}" ;;\n'
        "  esac\n"
        "  exit 0\n"
        "fi\n"
        "shift\n"
        'while [ "${1:-}" = -p ] || [ "${1:-}" = -f ]; do shift 2; done\n'
        'sub="${1:-}"; shift || true\n'
        'case "$sub" in\n'
        '  ps) printf "%s\\n" \'{"State":"exited","ExitCode":0}\' ;;\n'
        "  up) exit 0 ;;\n"
        "  exec)\n"
        '    [ "${1:-}" = -T ] && shift\n'
        '    while [ "${1:-}" = -e ]; do shift 2; done\n'
        "    shift || true\n"
        '    case "$*" in\n'
        "      true) exit 0 ;;\n"
        '      *"doctor check --json"*) printf "%s\\n" "$STUB_DOCTOR_JSON"; exit 1 ;;\n'
        '      *"notify send"*) cat >"$STUB_NOTIFY_FILE"; exit 0 ;;\n'
        "      *) exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_pass(tmp_path: Path, *, label: str = "1", **stub_env: str) -> str:
    """Source watchdog.sh, run one ``run_pass``, and return the captured owner DM (or "")."""
    bin_dir = tmp_path / "bin"
    _write_docker_stub(bin_dir)
    notify_file = tmp_path / f"dm-{label}.txt"
    harness = tmp_path / "harness.sh"
    harness.write_text(f'set -uo pipefail\nsource "{WATCHDOG}"\nrun_pass\n', encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_NOTIFY_FILE"] = str(notify_file)
    env["STUB_DOCKER_LOG"] = str(tmp_path / "docker.log")
    env.setdefault("STUB_CREATED", "")
    env.setdefault("STUB_DOCTOR_JSON", '{"ok": true, "findings": []}')
    # Shared across passes so the two-strikes ledger is exercised for real, and per-test
    # so a red pass here can never make another test's green pass announce a clear.
    env.setdefault("TEATREE_WATCHDOG_DEPLOY_PENDING_STATE", str(tmp_path / "pending.state"))
    env.setdefault("TEATREE_WATCHDOG_RED_STATE", str(tmp_path / "red.state"))
    env.setdefault("TEATREE_WATCHDOG_DEPLOY_LOCK", str(tmp_path / "absent-deploy.lock"))
    env.update(stub_env)
    subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=env)
    return notify_file.read_text(encoding="utf-8") if notify_file.exists() else ""


@contextlib.contextmanager
def _deploy_lock_held(lock: Path) -> Iterator[Path]:
    """Hold a REAL exclusive flock on *lock* for the body — what deploy.sh does."""
    lock.touch()
    holder = subprocess.Popen([_FLOCK or "flock", str(lock), "sleep", "60"])
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            probe = subprocess.run([_FLOCK or "flock", "-n", str(lock), "true"], check=False, capture_output=True)
            if probe.returncode != 0:
                break
            time.sleep(0.05)
        else:  # pragma: no cover — a flock that never lands would fail the assertions anyway
            pytest.fail("the lock holder never acquired the flock")
        yield lock
    finally:
        holder.kill()
        holder.wait()


class TestDeployInFlightSuppressesTheThreeFindings:
    """A convergence is running: the three findings it manufactures are not an outage.

    Every case runs TWO passes: one pass alone is silent under the two-strikes rule
    whatever the deploy probe says, so a single-pass assertion would hold even with
    the probe ripped out.
    """

    @pytest.mark.skipif(_FLOCK is None, reason="needs flock(1)")
    def test_held_deploy_lock_suppresses_all_three(self, tmp_path: Path) -> None:
        env = {"STUB_DOCTOR_JSON": _verdict(_FLOCK_FAIL, _LISTENER_FAIL, _CLONE_FAIL)}
        with _deploy_lock_held(tmp_path / "deploy.lock") as lock:
            env["TEATREE_WATCHDOG_DEPLOY_LOCK"] = str(lock)
            assert _run_pass(tmp_path, label="1", **env) == ""
            assert _run_pass(tmp_path, label="2", **env) == ""

    def test_a_just_recreated_container_suppresses_all_three(self, tmp_path: Path) -> None:
        # The image swap recreates containers; it is still settling.
        env = {
            "STUB_DOCTOR_JSON": _verdict(_FLOCK_FAIL, _LISTENER_FAIL, _CLONE_FAIL),
            "STUB_CREATED": _created(),
        }
        assert _run_pass(tmp_path, label="1", **env) == ""
        assert _run_pass(tmp_path, label="2", **env) == ""

    def test_the_creation_time_is_read_in_a_tzdata_free_format(self, tmp_path: Path) -> None:
        # `ps --format {{.CreatedAt}}` yields a local-zone abbreviation ("+0200
        # CEST") that GNU date REFUSES without tzdata — which the deploy image and
        # the CI image both lack, so the probe would silently never fire. Pin the
        # argv: the creation time comes from `inspect .Created` (RFC3339 UTC).
        _run_pass(tmp_path, STUB_DOCTOR_JSON=_verdict(_FLOCK_FAIL), STUB_CREATED=_created())
        log = (tmp_path / "docker.log").read_text(encoding="utf-8")
        assert "inspect --format {{.Created}}" in log
        assert "CreatedAt" not in log, "the tzdata-dependent human timestamp must never be parsed"

    def test_a_long_running_container_is_not_a_deploy(self, tmp_path: Path) -> None:
        # A crash-looping container RESTARTS without being recreated, so an old
        # creation timestamp must never read as a deploy — only the two-strikes
        # rule gates here, and the second pass pages.
        env = {"STUB_DOCTOR_JSON": _verdict(_FLOCK_FAIL), "STUB_CREATED": _created(seconds_ago=86400)}
        assert _run_pass(tmp_path, label="1", **env) == ""
        assert "holds the flock" in _run_pass(tmp_path, label="2", **env)


class TestTwoStrikesWithoutADeploy:
    """No deploy in flight: the first observation re-probes, the second pages."""

    def test_first_observation_does_not_page(self, tmp_path: Path) -> None:
        dm = _run_pass(tmp_path, STUB_DOCTOR_JSON=_verdict(_FLOCK_FAIL, _LISTENER_FAIL, _CLONE_FAIL))
        assert dm == ""

    def test_second_consecutive_observation_pages(self, tmp_path: Path) -> None:
        # The anti-vacuous case: a real freeze must still reach the owner. This
        # goes RED the moment the gating over-suppresses.
        env = {"STUB_DOCTOR_JSON": _verdict(_FLOCK_FAIL, _LISTENER_FAIL, _CLONE_FAIL)}
        assert _run_pass(tmp_path, label="1", **env) == ""
        dm = _run_pass(tmp_path, label="2", **env)
        assert "red findings" in dm
        assert "holds the flock" in dm
        assert "slack-listener receiver is DOWN" in dm
        assert "behind origin/main" in dm

    def test_the_ledger_keys_on_the_class_not_the_volatile_message(self, tmp_path: Path) -> None:
        # The clone-behind count changes between passes; a text-keyed ledger would
        # never match two observations and the finding could never page.
        assert _run_pass(tmp_path, label="1", STUB_DOCTOR_JSON=_verdict(_CLONE_FAIL)) == ""
        drifted = _CLONE_FAIL.replace("7 commit(s)", "9 commit(s)")
        assert "behind origin/main" in _run_pass(tmp_path, label="2", STUB_DOCTOR_JSON=_verdict(drifted))

    def test_a_green_pass_clears_the_ledger(self, tmp_path: Path) -> None:
        # "Two CONSECUTIVE passes": a green pass in between resets the count.
        red = _verdict(_FLOCK_FAIL)
        assert _run_pass(tmp_path, label="1", STUB_DOCTOR_JSON=red) == ""
        assert _run_pass(tmp_path, label="2", STUB_DOCTOR_JSON='{"ok": true, "findings": []}') == ""
        assert _run_pass(tmp_path, label="3", STUB_DOCTOR_JSON=red) == ""

    def test_a_deploy_pass_does_not_count_as_a_strike(self, tmp_path: Path) -> None:
        # A finding observed only while the swap ran is not evidence of anything.
        red = _verdict(_LISTENER_FAIL)
        assert _run_pass(tmp_path, label="1", STUB_DOCTOR_JSON=red, STUB_CREATED=_created()) == ""
        assert _run_pass(tmp_path, label="2", STUB_DOCTOR_JSON=red) == ""


class TestEveryOtherFindingPagesImmediately:
    """Only the three deploy-sensitive findings are gated; nothing else changes."""

    def test_an_unrelated_finding_pages_on_the_first_pass(self, tmp_path: Path) -> None:
        dm = _run_pass(tmp_path, STUB_DOCTOR_JSON=_verdict(_OTHER_FAIL))
        assert "red findings" in dm
        assert _OTHER_FAIL in dm

    def test_the_inverse_wedged_worker_finding_pages_on_the_first_pass(self, tmp_path: Path) -> None:
        # The near-miss a loose `holds the flock` pattern would swallow: the flock is
        # HELD and the loops are stalled. A deploy cannot manufacture that — it is a
        # wedged worker, and gating it would hide the outage the watchdog exists for.
        dm = _run_pass(tmp_path, STUB_DOCTOR_JSON=_verdict(_WEDGED_FAIL))
        assert _WEDGED_FAIL in dm

    @pytest.mark.skipif(_FLOCK is None, reason="needs flock(1)")
    def test_an_unrelated_finding_pages_even_during_a_deploy(self, tmp_path: Path) -> None:
        env = {"STUB_DOCTOR_JSON": _verdict(_FLOCK_FAIL, _OTHER_FAIL, _CLONE_FAIL)}
        with _deploy_lock_held(tmp_path / "deploy.lock") as lock:
            env["TEATREE_WATCHDOG_DEPLOY_LOCK"] = str(lock)
            _run_pass(tmp_path, label="1", **env)
            dm = _run_pass(tmp_path, label="2", **env)
        assert _OTHER_FAIL in dm
        assert "holds the flock" not in dm, "the deploy-sensitive findings stay gated"
        assert "behind origin/main" not in dm

    def test_a_red_verdict_with_no_extractable_finding_still_pages(self, tmp_path: Path) -> None:
        # Never silently drop a red verdict: nothing to classify → the generic body.
        dm = _run_pass(tmp_path, STUB_DOCTOR_JSON='{"ok": false, "findings": []}')
        assert "red findings" in dm


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
