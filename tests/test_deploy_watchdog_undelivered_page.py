# test-path: cross-cutting — drives deploy/watchdog.sh (no src mirror).
"""An owner page the transport could not deliver is parked, never lost (#4339).

The watchdog detected the outage, tried to notify, and logged `could not deliver
owner DM (Slack may be unwired on this box)`. The one channel that would have
surfaced the failure was itself broken, so the detection was effectively silent and
the log line scrolled away. An undeliverable page is its own failure: the body is
persisted with its idempotency key, re-surfaced on every later pass, and re-delivered
the moment the channel recovers.

Runs the REAL ``run_pass`` (the script is sourced, its dispatch guarded so it does not
auto-run) against a stub ``docker`` whose ``notify send`` exec fails while
``STUB_NOTIFY_RC`` is non-zero.
"""

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

_GREEN = '{"ok": true, "findings": []}'
_FAIL_MESSAGE = "Compose service teatree-worker is exited"
_RED = f'{{"ok": false, "findings": [{{"level": "FAIL", "message": "{_FAIL_MESSAGE}"}}]}}'


def _write_docker_stub(bin_dir: Path) -> None:
    """A ``docker`` shim whose ``notify send`` exec fails while ``STUB_NOTIFY_RC`` is set."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" != compose ]; then exit 0; fi\n'
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
        '      *"notify send"*)\n'
        '        if [ "${STUB_NOTIFY_RC:-0}" != 0 ]; then exit "$STUB_NOTIFY_RC"; fi\n'
        '        cat >>"$STUB_NOTIFY_FILE"; printf "\\n" >>"$STUB_NOTIFY_FILE" ;;\n'
        "      *) exit 0 ;;\n"
        "    esac\n"
        "    ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_pass(tmp_path: Path, **stub_env: str) -> tuple[str, str]:
    """Source watchdog.sh, run one ``run_pass``, and return (every owner DM, the log)."""
    bin_dir = tmp_path / "bin"
    _write_docker_stub(bin_dir)
    notify_file = tmp_path / "dm.txt"
    harness = tmp_path / "harness.sh"
    harness.write_text(f'set -uo pipefail\nsource "{WATCHDOG}"\nrun_pass\n', encoding="utf-8")

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_NOTIFY_FILE"] = str(notify_file)
    env["TEATREE_WATCHDOG_DOCTOR_RETRY_DELAY"] = "0"
    # Shared across passes so the ledger is exercised for real, per-test so no other
    # test's parked page can leak in.
    env["TEATREE_WATCHDOG_UNDELIVERED_STATE"] = str(tmp_path / "undelivered.state")
    env["TEATREE_WATCHDOG_RED_STATE"] = str(tmp_path / "red.state")
    env["TEATREE_WATCHDOG_DEPLOY_PENDING_STATE"] = str(tmp_path / "pending.state")
    env["TEATREE_WATCHDOG_LIVENESS_STATE"] = str(tmp_path / "liveness.state")
    env["TEATREE_WATCHDOG_DEPLOY_LOCK"] = str(tmp_path / "absent.lock")
    env.setdefault("STUB_DOCTOR_JSON", _RED)
    env.update(stub_env)
    done = subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=env)
    dms = notify_file.read_text(encoding="utf-8") if notify_file.exists() else ""
    return dms, done.stderr


def _ledger(tmp_path: Path) -> str:
    path = tmp_path / "undelivered.state"
    return path.read_text(encoding="utf-8") if path.exists() else ""


class TestAnUndeliverablePageIsParked:
    def test_the_page_is_persisted_with_its_key(self, tmp_path: Path) -> None:
        dms, log = _run_pass(tmp_path, STUB_NOTIFY_RC="1")

        assert dms == "", "the transport was down — nothing can have been delivered"
        assert "watchdog:red:" in _ledger(tmp_path), "the page must outlive the pass that raised it"
        assert "UNDELIVERED" in log

    def test_a_delivered_page_is_never_parked(self, tmp_path: Path) -> None:
        # The anti-vacuous control: parking must not fire on the healthy path.
        dms, _log = _run_pass(tmp_path)

        assert _FAIL_MESSAGE in dms
        assert _ledger(tmp_path) == ""

    def test_the_ledger_is_re_surfaced_every_pass_until_it_drains(self, tmp_path: Path) -> None:
        _run_pass(tmp_path, STUB_NOTIFY_RC="1")

        _dms, log = _run_pass(tmp_path, STUB_DOCTOR_JSON=_GREEN, STUB_NOTIFY_RC="1")

        assert "STILL UNDELIVERED" in log, "a parked page must not scroll away"

    def test_the_parked_page_is_delivered_once_the_channel_recovers(self, tmp_path: Path) -> None:
        _run_pass(tmp_path, STUB_NOTIFY_RC="1")

        dms, _log = _run_pass(tmp_path, STUB_DOCTOR_JSON=_GREEN)

        assert _FAIL_MESSAGE in dms, "the page raised while the channel was down must still arrive"
        assert _ledger(tmp_path) == "", "a delivered page must leave the ledger"

    def test_the_ledger_is_bounded(self, tmp_path: Path) -> None:
        ledger = tmp_path / "undelivered.state"
        ledger.write_text("".join(f"1 old-key-{n} Ym9keQ==\n" for n in range(80)), encoding="utf-8")

        _run_pass(tmp_path, STUB_NOTIFY_RC="1", TEATREE_WATCHDOG_UNDELIVERED_MAX="10")

        assert len(_ledger(tmp_path).splitlines()) <= 10


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
