# test-path: cross-cutting — drives deploy/watchdog.sh (no src mirror).
"""The watchdog announces the outage it repaired (deploy/watchdog.sh, #3901).

The worker was gone for four hours and the owner found out by looking at the
dashboard. The watchdog had already restarted the stack, and the doctor pass that
followed was green — so `up -d --no-recreate` healed the box in silence and nothing
recorded that anything had been wrong. A silent auto-heal is indistinguishable from
a healthy idle factory.

These run the REAL `run_pass` (the script is sourced, its dispatch guarded so it does
not auto-run) against a stub `docker` whose reported container states CHANGE once
`compose up` has run, so both the recovered and the still-down branches are driven for
real rather than asserted from one frozen snapshot. Every DM is appended, so a pass
that sends both the repair notice and a doctor-red notice can be asserted on both.
"""

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

WATCHDOG = Path(__file__).resolve().parents[1] / "deploy" / "watchdog.sh"
_BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None or shutil.which("python3") is None,
    reason="needs bash + jq + python3 (present in the deploy image and CI)",
)

_GREEN = '{"ok": true, "findings": []}'
_RED = '{"ok": false, "findings": [{"level": "FAIL", "message": "Compose service teatree-worker is exited"}]}'

_ALL_UP = "\n".join(
    f"{svc}\trunning\tUp 8 hours"
    for svc in ("teatree-worker", "teatree-admin", "teatree-slack-listener", "teatree-watchdog")
)
_WORKER_DOWN = _ALL_UP.replace("teatree-worker\trunning\tUp 8 hours", "teatree-worker\texited\tExited (0) 4 hours ago")


def _write_docker_stub(bin_dir: Path) -> None:
    """A `docker` shim whose reported states flip once `compose up` has run.

    `STUB_PS_BEFORE` is served until a `compose up` touches `STUB_UP_MARKER`, then
    `STUB_PS_AFTER` is — modelling the restart the watchdog performs mid-pass, which
    is what its post-restart re-read is supposed to observe. `STUB_IDS` drives the
    container-creation probe (empty → no containers → no deploy in flight), whose
    `inspect` row carries the creation time plus the restart count and state that tell
    a settling swap from a crash loop. A `notify send` exec APPENDS the piped DM body
    so multiple DMs in one pass survive.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" != compose ]; then\n'
        '  if [ "$1" = ps ]; then\n'
        '    case "$*" in\n'
        '      *.ID*) printf "%s\\n" "${STUB_IDS:-}" ;;\n'
        '      *) if [ -f "${STUB_UP_MARKER:-/nonexistent}" ]; then\n'
        '           printf "%s\\n" "${STUB_PS_AFTER:-}"\n'
        "         else\n"
        '           printf "%s\\n" "${STUB_PS_BEFORE:-}"\n'
        "         fi ;;\n"
        "    esac\n"
        "  fi\n"
        '  if [ "$1" = inspect ] && [ -n "${STUB_CREATED:-}" ]; then\n'
        '    printf "%s\\t%s\\t%s\\n" "$STUB_CREATED" "${STUB_RESTARTS:-0}" "${STUB_STATUS:-running}"\n'
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        "shift\n"
        'while [ "${1:-}" = -p ] || [ "${1:-}" = -f ]; do shift 2; done\n'
        'sub="${1:-}"; shift || true\n'
        'case "$sub" in\n'
        '  ps) printf "%s\\n" \'{"State":"exited","ExitCode":0}\' ;;\n'
        '  up) : >"${STUB_UP_MARKER:-/dev/null}"; exit 0 ;;\n'
        "  exec)\n"
        '    [ "${1:-}" = -T ] && shift\n'
        '    while [ "${1:-}" = -e ]; do shift 2; done\n'
        "    shift || true\n"
        '    case "$*" in\n'
        "      true) exit 0 ;;\n"
        '      *"doctor check --json"*) printf "%s\\n" "$STUB_DOCTOR_JSON"; exit "${STUB_DOCTOR_RC:-0}" ;;\n'
        '      *"notify send"*) cat >>"$STUB_NOTIFY_FILE"; printf "\\n" >>"$STUB_NOTIFY_FILE"; exit 0 ;;\n'
        "      *) exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_pass(tmp_path: Path, *, label: str = "1", **stub_env: str) -> str:
    """Source watchdog.sh, run one `run_pass`, and return every owner DM it sent."""
    bin_dir = tmp_path / "bin"
    _write_docker_stub(bin_dir)
    notify_file = tmp_path / "dm.txt"
    harness = tmp_path / "harness.sh"
    harness.write_text(f'set -uo pipefail\nsource "{WATCHDOG}"\nrun_pass\n', encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_NOTIFY_FILE"] = str(notify_file)
    # Per-pass, so a second pass in the same test re-samples the pre-restart state.
    env["STUB_UP_MARKER"] = str(tmp_path / f"up-{label}.marker")
    env["TEATREE_WATCHDOG_DOCTOR_RETRY_DELAY"] = "0"
    env["TEATREE_WATCHDOG_RED_STATE"] = str(tmp_path / "red.state")
    env["TEATREE_WATCHDOG_DEPLOY_PENDING_STATE"] = str(tmp_path / "pending.state")
    env["TEATREE_WATCHDOG_LIVENESS_STATE"] = str(tmp_path / "liveness.state")
    env["TEATREE_WATCHDOG_DEPLOY_LOCK"] = str(tmp_path / "absent.lock")
    env.setdefault("STUB_DOCTOR_JSON", _GREEN)
    env.setdefault("STUB_PS_BEFORE", _ALL_UP)
    env.setdefault("STUB_PS_AFTER", _ALL_UP)
    env.setdefault("STUB_IDS", "")
    env.setdefault("STUB_CREATED", "")
    env.update(stub_env)
    notify_file.unlink(missing_ok=True)
    subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=env)
    return notify_file.read_text(encoding="utf-8") if notify_file.exists() else ""


def _seed_liveness(tmp_path: Path, *, service: str, minutes_ago: int) -> None:
    ledger = tmp_path / "liveness.state"
    ledger.write_text(f"{service} {int(time.time()) - minutes_ago * 60}\n", encoding="utf-8")


class TestRepairIsAnnounced:
    def test_a_service_the_watchdog_restarted_is_dmed(self, tmp_path: Path) -> None:
        dm = _run_pass(tmp_path, STUB_PS_BEFORE=_WORKER_DOWN, STUB_PS_AFTER=_ALL_UP)

        assert "teatree-worker" in dm
        assert "restarted" in dm
        assert "STILL DOWN" not in dm

    def test_a_healthy_pass_announces_nothing(self, tmp_path: Path) -> None:
        assert _run_pass(tmp_path, STUB_PS_BEFORE=_ALL_UP, STUB_PS_AFTER=_ALL_UP) == ""

    def test_a_service_that_did_not_come_back_is_reported_still_down(self, tmp_path: Path) -> None:
        dm = _run_pass(tmp_path, STUB_PS_BEFORE=_WORKER_DOWN, STUB_PS_AFTER=_WORKER_DOWN)

        assert "STILL DOWN" in dm

    def test_an_unreadable_daemon_announces_nothing_rather_than_guessing(self, tmp_path: Path) -> None:
        """No `docker ps` output is "cannot tell", never "every service is down"."""
        assert _run_pass(tmp_path, STUB_PS_BEFORE="", STUB_PS_AFTER="") == ""

    def test_the_doctor_verdict_still_pages_alongside_the_repair(self, tmp_path: Path) -> None:
        dm = _run_pass(
            tmp_path,
            STUB_PS_BEFORE=_WORKER_DOWN,
            STUB_PS_AFTER=_ALL_UP,
            STUB_DOCTOR_JSON=_RED,
            STUB_DOCTOR_RC="1",
        )

        assert "restarted" in dm
        assert "red findings" in dm


class TestDowntimeIsAnswerable:
    def test_the_dm_names_how_long_the_service_had_been_gone(self, tmp_path: Path) -> None:
        _seed_liveness(tmp_path, service="teatree-worker", minutes_ago=245)

        dm = _run_pass(tmp_path, STUB_PS_BEFORE=_WORKER_DOWN, STUB_PS_AFTER=_ALL_UP)

        assert "down ~245 min" in dm

    def test_a_never_seen_service_says_so_rather_than_inventing_a_duration(self, tmp_path: Path) -> None:
        dm = _run_pass(tmp_path, STUB_PS_BEFORE=_WORKER_DOWN, STUB_PS_AFTER=_ALL_UP)

        assert "down for an unknown period" in dm

    def test_a_healthy_pass_stamps_every_service_so_the_next_outage_is_measurable(self, tmp_path: Path) -> None:
        _run_pass(tmp_path, STUB_PS_BEFORE=_ALL_UP, STUB_PS_AFTER=_ALL_UP)

        ledger = (tmp_path / "liveness.state").read_text(encoding="utf-8")
        stamped = {line.split()[0] for line in ledger.splitlines() if len(line.split()) == 2}
        assert "teatree-worker" in stamped
        assert "teatree-admin" in stamped

    def test_a_recovered_service_is_restamped_so_the_next_outage_measures_from_recovery(self, tmp_path: Path) -> None:
        """Stamping must follow the POST-restart state, or every later outage inherits this one."""
        _seed_liveness(tmp_path, service="teatree-worker", minutes_ago=300)
        recovered = _run_pass(tmp_path, label="1", STUB_PS_BEFORE=_WORKER_DOWN, STUB_PS_AFTER=_ALL_UP)
        assert "down ~300 min" in recovered

        again = _run_pass(tmp_path, label="2", STUB_PS_BEFORE=_WORKER_DOWN, STUB_PS_AFTER=_ALL_UP)

        assert "down ~0 min" in again
        assert "down ~300 min" not in again

    def test_a_down_service_keeps_its_earlier_stamp_across_passes(self, tmp_path: Path) -> None:
        _seed_liveness(tmp_path, service="teatree-worker", minutes_ago=120)
        _run_pass(tmp_path, label="1", STUB_PS_BEFORE=_WORKER_DOWN, STUB_PS_AFTER=_WORKER_DOWN)

        dm = _run_pass(tmp_path, label="2", STUB_PS_BEFORE=_WORKER_DOWN, STUB_PS_AFTER=_WORKER_DOWN)

        assert "down ~120 min" in dm


class TestDeployWindowDoesNotPage:
    def test_a_recently_recreated_stack_is_not_reported_as_an_outage(self, tmp_path: Path) -> None:
        """A rolling swap legitimately stops containers — that is a deploy, not an outage."""
        created = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() - 5)) + ".683288764Z"

        dm = _run_pass(
            tmp_path,
            STUB_PS_BEFORE=_WORKER_DOWN,
            STUB_PS_AFTER=_ALL_UP,
            STUB_IDS="abc123",
            STUB_CREATED=created,
        )

        assert "restarted" not in dm
