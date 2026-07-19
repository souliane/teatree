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
    is the reachability probe's exit code (non-zero → unreachable); a doctor `exec`
    prints ``STUB_DOCTOR_JSON`` and exits ``STUB_DOCTOR_RC``; a `notify send` exec
    captures the piped DM body to ``STUB_NOTIFY_FILE``.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        '[ "$1" = compose ] || exit 0\n'
        "shift\n"
        'while [ "${1:-}" = -p ] || [ "${1:-}" = -f ]; do shift 2; done\n'
        'sub="${1:-}"; shift || true\n'
        'case "$sub" in\n'
        '  ps) printf "%s\\n" "$STUB_INIT_PS" ;;\n'
        "  up) exit 0 ;;\n"
        "  exec)\n"
        '    [ "${1:-}" = -T ] && shift\n'
        "    shift || true\n"
        '    rest="$*"\n'
        '    case "$rest" in\n'
        '      true) exit "${STUB_TRUE_RC:-0}" ;;\n'
        '      *"doctor check --json"*) printf "%s\\n" "$STUB_DOCTOR_JSON"; exit "${STUB_DOCTOR_RC:-1}" ;;\n'
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
    env.setdefault("STUB_INIT_PS", '{"State":"exited","ExitCode":0}')
    env.setdefault("STUB_DOCTOR_JSON", _GREEN_VERDICT)
    env.update(stub_env)
    subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=False, env=env)
    return notify_file.read_text(encoding="utf-8") if notify_file.exists() else ""


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


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
