"""The in-daemon watchdog's red-findings DM path (deploy/watchdog.sh, #3440).

`t3 doctor check --json` exits NON-ZERO when it ran and found red findings (it
still prints a parseable ``{"ok": false, ...}`` verdict). The watchdog used to
read any non-zero exit as "stack unreachable", so a genuine red verdict was
misreported as unreachable and the findings-DM path was dead code. The fix keys
on the PRESENCE of a parseable verdict, not the exit code.

These run the REAL `run_pass` (the script is sourced, its dispatch guarded so it
does not auto-run) with a stub `docker` on PATH that models `docker compose`:
init complete, the stack reachable, and `t3 doctor` emitting a chosen verdict.
The owner DM is captured to a file so each branch's message is asserted.
"""

import base64
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

WATCHDOG = Path(__file__).resolve().parents[1] / "deploy" / "watchdog.sh"
_BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None or shutil.which("python3") is None,
    reason="needs bash + jq + python3 (present in the deploy image and CI)",
)

_RED_VERDICT = '{"ok": false, "findings": [{"level": "FAIL", "message": "Compose service teatree-worker is exited"}]}'
_GREEN_VERDICT = '{"ok": true, "findings": []}'


def _write_docker_stub(bin_dir: Path) -> None:
    """A `docker` shim modelling the `docker compose` calls run_pass makes.

    ``STUB_INIT_PS`` is the init service's `ps --format json` row; ``STUB_TRUE_RC``
    is the reachability probe's exit code (non-zero → unreachable, with
    ``STUB_TRUE_STDERR`` on stderr); a doctor `exec` prints ``STUB_DOCTOR_JSON`` and
    exits ``STUB_DOCTOR_RC``; a `notify send` exec captures the piped DM body to
    ``STUB_NOTIFY_FILE``.

    ``STUB_UNREACHABLE_SVC`` makes the reachability probe fail for exactly that one
    service, so the fallback ordering can be driven. ``STUB_DOCTOR_TRANSIENT_UNTIL``
    makes the first N doctor attempts fail the way a restarting target does — nothing
    on stdout, ``STUB_DOCTOR_STDERR`` on stderr — so a bounded retry can be driven;
    attempts are counted into ``STUB_DOCTOR_ATTEMPTS``.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        # A bare `docker ps` (NOT `docker compose ps`) is the watchdog's socket-only
        # compose-state gather — serve STUB_DOCKER_PS so the handoff can be asserted.
        'if [ "$1" != compose ]; then\n'
        '  [ "$1" = ps ] && printf "%s\\n" "$STUB_DOCKER_PS"\n'
        "  exit 0\n"
        "fi\n"
        "shift\n"
        'while [ "${1:-}" = -p ] || [ "${1:-}" = -f ]; do shift 2; done\n'
        'sub="${1:-}"; shift || true\n'
        'case "$sub" in\n'
        '  ps) printf "%s\\n" "$STUB_INIT_PS" ;;\n'
        "  up) exit 0 ;;\n"
        "  exec)\n"
        '    printf "%s\\n" "$*" >>"${STUB_EXEC_LOG:-/dev/null}"\n'
        '    [ "${1:-}" = -T ] && shift\n'
        '    while [ "${1:-}" = -e ]; do shift 2; done\n'
        '    svc="${1:-}"\n'
        "    shift || true\n"
        '    rest="$*"\n'
        '    case "$rest" in\n'
        "      true)\n"
        '        rc="${STUB_TRUE_RC:-0}"\n'
        '        [ -z "${STUB_UNREACHABLE_SVC:-}" ] || { rc=0; [ "$svc" != "$STUB_UNREACHABLE_SVC" ] || rc=1; }\n'
        '        [ "$rc" = 0 ] || printf "%s\\n" "${STUB_TRUE_STDERR:-}" >&2\n'
        '        exit "$rc" ;;\n'
        '      *"doctor check --json"*)\n'
        "        n=0\n"
        '        if [ -n "${STUB_DOCTOR_ATTEMPTS:-}" ]; then\n'
        '          n=$(cat "$STUB_DOCTOR_ATTEMPTS" 2>/dev/null || printf 0)\n'
        "          n=$((n + 1))\n"
        '          printf "%s" "$n" >"$STUB_DOCTOR_ATTEMPTS"\n'
        "        fi\n"
        '        if [ "$n" -le "${STUB_DOCTOR_TRANSIENT_UNTIL:-0}" ]; then\n'
        '          printf "%s\\n" "${STUB_DOCTOR_STDERR:-}" >&2\n'
        "          exit 1\n"
        "        fi\n"
        '        printf "%s\\n" "$STUB_DOCTOR_JSON"; exit "${STUB_DOCTOR_RC:-1}" ;;\n'
        '      *"notify send"*) cat >"$STUB_NOTIFY_FILE"; exit 0 ;;\n'
        "      *) exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_pass(tmp_path: Path, **stub_env: str) -> str:
    """Source watchdog.sh, run one `run_pass`, and return the captured owner DM (or "")."""
    bin_dir = tmp_path / "bin"
    _write_docker_stub(bin_dir)
    notify_file = tmp_path / "dm.txt"
    harness = tmp_path / "harness.sh"
    harness.write_text(f'set -uo pipefail\nsource "{WATCHDOG}"\nrun_pass\n', encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_NOTIFY_FILE"] = str(notify_file)
    env["STUB_EXEC_LOG"] = str(tmp_path / "exec.log")
    env["STUB_DOCTOR_ATTEMPTS"] = str(tmp_path / "attempts.txt")
    env["TEATREE_WATCHDOG_DOCTOR_RETRY_DELAY"] = "0"
    env.setdefault("STUB_INIT_PS", '{"State":"exited","ExitCode":0}')
    env.setdefault("STUB_DOCTOR_JSON", _GREEN_VERDICT)
    env.update(stub_env)
    subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=env)
    return notify_file.read_text(encoding="utf-8") if notify_file.exists() else ""


def _exec_log(tmp_path: Path) -> str:
    log = tmp_path / "exec.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _doctor_exec_lines(tmp_path: Path) -> list[str]:
    return [line for line in _exec_log(tmp_path).splitlines() if "doctor check --json" in line]


def _doctor_attempts(tmp_path: Path) -> int:
    counter = tmp_path / "attempts.txt"
    return int(counter.read_text(encoding="utf-8")) if counter.exists() else 0


_RESTARTING_ERR = "Error response from daemon: Container 9f2c is restarting, wait until the container is running"


class TestWatchdogRunPass:
    def test_red_findings_verdict_dms_the_findings_not_unreachable(self, tmp_path: Path) -> None:
        # doctor RAN and found reds → exit 1 WITH a parseable verdict. This must
        # take the findings-DM path (the #3440 regression sent "unreachable").
        dm = _run_pass(tmp_path, STUB_DOCTOR_JSON=_RED_VERDICT, STUB_DOCTOR_RC="1")
        assert "red findings" in dm
        assert "teatree-worker" in dm, "the FAIL message must be in the DM body"
        assert "unreachable" not in dm

    def test_green_verdict_sends_no_dm(self, tmp_path: Path) -> None:
        dm = _run_pass(tmp_path, STUB_DOCTOR_JSON=_GREEN_VERDICT, STUB_DOCTOR_RC="0")
        assert dm == ""

    def test_unreachable_stack_dms_unreachable(self, tmp_path: Path) -> None:
        # No exec service is reachable (the probe fails) → the ONLY true-unreachable case.
        dm = _run_pass(tmp_path, STUB_TRUE_RC="1")
        assert "unreachable" in dm

    def test_reachable_but_no_verdict_is_treated_as_red(self, tmp_path: Path) -> None:
        # doctor ran but emitted no parseable verdict — a half-crashed doctor is a
        # RED condition, not a silent healthy pass.
        dm = _run_pass(tmp_path, STUB_DOCTOR_JSON="garbage with no verdict line", STUB_DOCTOR_RC="1")
        assert "no parseable verdict" in dm.lower()


class TestWatchdogComposeStateHandoff:
    """The socket-holding watchdog hands compose states to the socket-less doctor.

    `t3 doctor` runs in an app container with the `docker` CLI but no
    `/var/run/docker.sock`, so its own compose-stack probe cannot reach the daemon.
    The watchdog (the ONE container with the socket) gathers `docker ps` and passes
    it to the doctor via `-e TEATREE_DOCTOR_COMPOSE_PS=<base64>`, so the detector
    actually runs. Without the handoff the detector is dead code in production.
    """

    def test_docker_ps_states_forwarded_to_doctor_as_base64(self, tmp_path: Path) -> None:
        ps_rows = "teatree-init\texited\tExited (1) 2 minutes ago"
        _run_pass(tmp_path, STUB_DOCKER_PS=ps_rows, STUB_DOCTOR_JSON=_GREEN_VERDICT, STUB_DOCTOR_RC="0")
        expected = base64.b64encode(ps_rows.encode("utf-8")).decode("ascii")
        exec_log = _exec_log(tmp_path)
        assert f"TEATREE_DOCTOR_COMPOSE_PS={expected}" in exec_log

    def test_handoff_flag_present_even_when_states_empty(self, tmp_path: Path) -> None:
        # An empty `docker ps` still forwards the (empty) handoff, never omitting the
        # flag — so the exec shape is stable and the doctor falls back cleanly.
        _run_pass(tmp_path, STUB_DOCKER_PS="", STUB_DOCTOR_JSON=_GREEN_VERDICT, STUB_DOCTOR_RC="0")
        assert "TEATREE_DOCTOR_COMPOSE_PS=" in _exec_log(tmp_path)


class TestWatchdogTargetsTheWorker:
    """The health probe runs where heavy work belongs, not in the lean web container.

    `t3 doctor check --json` boots Django, scans the DB and makes live third-party
    HTTP calls. Running it inside the 512m admin — which is simultaneously serving
    the dashboard — left ~130 MiB of headroom and restarted the container every
    watchdog pass (#3651). The worker is sized for heavy work, so it is probed first.
    """

    def test_doctor_probe_targets_the_worker_first(self, tmp_path: Path) -> None:
        _run_pass(tmp_path, STUB_DOCTOR_JSON=_GREEN_VERDICT, STUB_DOCTOR_RC="0")
        assert "teatree-worker" in _doctor_exec_lines(tmp_path)[0]

    def test_admin_is_the_fallback_when_the_worker_is_unreachable(self, tmp_path: Path) -> None:
        # A down worker must not blind the watchdog — the admin stays in the list.
        dm = _run_pass(
            tmp_path,
            STUB_UNREACHABLE_SVC="teatree-worker",
            STUB_DOCTOR_JSON=_GREEN_VERDICT,
            STUB_DOCTOR_RC="0",
        )
        assert dm == ""
        assert "teatree-admin" in _doctor_exec_lines(tmp_path)[0]


class TestWatchdogTransientTargetUnavailability:
    """A target that was restarting is a transient to retry, not a red to page on.

    A completed doctor run that emits nothing is still RED (a half-crashed doctor).
    A probe that could not run at all — the daemon refusing the exec because the
    container is restarting/not running — is a transient: retried, never paged.
    """

    def test_restarting_target_is_retried_and_does_not_page(self, tmp_path: Path) -> None:
        dm = _run_pass(
            tmp_path,
            STUB_DOCTOR_TRANSIENT_UNTIL="99",
            STUB_DOCTOR_STDERR=_RESTARTING_ERR,
            TEATREE_WATCHDOG_DOCTOR_RETRIES="3",
        )
        assert dm == ""
        assert _doctor_attempts(tmp_path) == 3

    def test_transient_that_clears_on_retry_reads_the_recovered_verdict(self, tmp_path: Path) -> None:
        dm = _run_pass(
            tmp_path,
            STUB_DOCTOR_TRANSIENT_UNTIL="1",
            STUB_DOCTOR_STDERR=_RESTARTING_ERR,
            STUB_DOCTOR_JSON=_RED_VERDICT,
            STUB_DOCTOR_RC="1",
            TEATREE_WATCHDOG_DOCTOR_RETRIES="3",
        )
        assert "red findings" in dm
        assert _doctor_attempts(tmp_path) == 2

    def test_unreachable_service_with_a_restarting_error_does_not_page(self, tmp_path: Path) -> None:
        # The exec never lands because every target is mid-restart — transient, not
        # the genuine "stack unreachable" outage.
        dm = _run_pass(
            tmp_path,
            STUB_TRUE_RC="1",
            STUB_TRUE_STDERR=_RESTARTING_ERR,
            TEATREE_WATCHDOG_DOCTOR_RETRIES="2",
        )
        assert dm == ""

    def test_completed_run_with_no_verdict_still_pages_as_red(self, tmp_path: Path) -> None:
        # The deliberate #3440 behaviour must survive the transient carve-out: doctor
        # RAN (no daemon error) and produced nothing → still RED.
        dm = _run_pass(tmp_path, STUB_DOCTOR_JSON="garbage with no verdict line", STUB_DOCTOR_RC="1")
        assert "no parseable verdict" in dm.lower()
        assert _doctor_attempts(tmp_path) == 1, "a completed run must not be retried"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
