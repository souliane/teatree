# test-path: cross-cutting — drives deploy/watchdog.sh (no src mirror).
"""The watchdog's periodic stale-temp trim (deploy/watchdog.sh).

Runtime temp is routed to disk, but a crashed/abandoned run can still leak
pytest/uv/claude scratch that grows unbounded and eventually fills the disk. Each
watchdog pass runs ``trim_stale_temp``, which `exec`s a bounded, age-gated,
name-scoped ``find ... -exec rm -rf`` into each configured service so the leak can
never wedge the box.

Runs the REAL ``trim_stale_temp`` (the script is sourced, its dispatch guarded so
it does not auto-run) with a stub ``docker`` that records every ``compose exec``
command, so the scope of what gets deleted is asserted rather than reimplemented.
"""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

WATCHDOG = Path(__file__).resolve().parents[1] / "deploy" / "watchdog.sh"
_BASH = shutil.which("bash") or "bash"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")


def _write_docker_stub(bin_dir: Path, record: Path) -> None:
    """A ``docker`` shim that appends each ``compose exec`` invocation to *record*."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "docker"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        '[ "$1" = compose ] || exit 0\n'
        "shift\n"
        'while [ "${1:-}" = -p ] || [ "${1:-}" = -f ]; do shift 2; done\n'
        'sub="${1:-}"; shift || true\n'
        'case "$sub" in\n'
        "  exec)\n"
        '    [ "${1:-}" = -T ] && shift\n'
        f'    printf "%s\\n" "$*" >>{str(record)!r}\n'
        "    exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _run_trim(tmp_path: Path, **env: str) -> list[str]:
    """Source the watchdog and run ``trim_stale_temp``; return the recorded exec commands."""
    bin_dir = tmp_path / "bin"
    record = tmp_path / "exec.log"
    record.write_text("", encoding="utf-8")
    _write_docker_stub(bin_dir, record)
    harness = tmp_path / "harness.sh"
    harness.write_text(f"set -euo pipefail\nsource {str(WATCHDOG)!r}\ntrim_stale_temp\n", encoding="utf-8")
    run_env = dict(os.environ)
    run_env["PATH"] = f"{bin_dir}{os.pathsep}{run_env['PATH']}"
    run_env.update(env)
    subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=True, env=run_env)
    return [line for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]


class TestTrimStaleTemp:
    def test_sweeps_each_service_and_root_with_scoped_age_gated_delete(self, tmp_path: Path) -> None:
        commands = _run_trim(
            tmp_path,
            TEATREE_WATCHDOG_TEMP_TRIM_SERVICES="teatree-worker teatree-admin",
            TEATREE_WATCHDOG_TEMP_TRIM_ROOTS="/var/tmp /tmp",
            TEATREE_WATCHDOG_TEMP_TRIM_MIN_AGE_MIN="720",
        )
        # 2 services x 2 roots.
        assert len(commands) == 4
        joined = "\n".join(commands)
        for root in ("/var/tmp", "/tmp"):
            assert f"find '{root}'" in joined
        # Scoped to known scratch only, age-gated, and a bounded delete.
        for name in ("pytest-*", "uv-*", "claude-*"):
            assert f"-name '{name}'" in joined
        assert "-mmin +720" in joined
        assert "-exec rm -rf {} +" in joined
        # NEVER an unscoped whole-dir wipe.
        assert "rm -rf /var/tmp " not in joined
        assert "rm -rf /tmp " not in joined

    def test_honors_custom_age_and_single_service(self, tmp_path: Path) -> None:
        commands = _run_trim(
            tmp_path,
            TEATREE_WATCHDOG_TEMP_TRIM_SERVICES="teatree-worker",
            TEATREE_WATCHDOG_TEMP_TRIM_ROOTS="/var/tmp",
            TEATREE_WATCHDOG_TEMP_TRIM_MIN_AGE_MIN="60",
        )
        assert len(commands) == 1
        assert "-mmin +60" in commands[0]
