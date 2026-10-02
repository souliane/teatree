# test-path: cross-cutting — drives deploy/entrypoint.sh (no src mirror).
"""The deploy entrypoint routes runtime temp to DISK, off the box's RAM tmpfs.

`deploy/entrypoint.sh`'s ``setup_disk_tmpdir`` exports ``TMPDIR`` /
``PYTEST_DEBUG_TEMPROOT`` to a disk-backed root (``/var/tmp`` by default) and
creates it, BEFORE each role `exec`s — so the spawned headless ``claude``, pytest,
and uv land their scratch on disk instead of the small RAM-backed ``/tmp`` that
otherwise fills to ENOSPC and wedges the box.

Runs the REAL shell function (extracted verbatim from the entrypoint) in a bash
subprocess, so the export + mkdir contract is exercised end to end.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash (present in the deploy image and CI)")

ENTRYPOINT = Path(__file__).resolve().parents[1] / "deploy" / "entrypoint.sh"
_BASH = shutil.which("bash") or "bash"


def _extract_shell_function(name: str) -> str:
    """Return the verbatim source of shell function *name* from the entrypoint."""
    body: list[str] = []
    capturing = False
    for line in ENTRYPOINT.read_text(encoding="utf-8").splitlines():
        if line.startswith(f"{name}() {{"):
            capturing = True
        if capturing:
            body.append(line)
            if line == "}":
                return "\n".join(body)
    not_found = f"function {name!r} not found in {ENTRYPOINT}"
    raise AssertionError(not_found)


def _run_setup(tmp_path: Path, **env: str) -> dict[str, str]:
    """Run ``setup_disk_tmpdir`` and return the resulting TMPDIR/PYTEST_DEBUG_TEMPROOT."""
    func = _extract_shell_function("setup_disk_tmpdir")
    harness = tmp_path / "harness.sh"
    harness.write_text(
        f"set -euo pipefail\n{func}\nsetup_disk_tmpdir\n"
        'printf "TMPDIR=%s\\n" "${TMPDIR:-}"\nprintf "PYTEST=%s\\n" "${PYTEST_DEBUG_TEMPROOT:-}"\n',
        encoding="utf-8",
    )
    run_env = dict(os.environ)
    run_env.pop("TMPDIR", None)
    run_env.pop("PYTEST_DEBUG_TEMPROOT", None)
    run_env.update(env)
    proc = subprocess.run([_BASH, str(harness)], capture_output=True, text=True, check=True, env=run_env)
    return dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)


class TestSetupDiskTmpdir:
    def test_defaults_to_var_tmp_off_the_tmpfs(self, tmp_path: Path) -> None:
        result = _run_setup(tmp_path)
        assert result["TMPDIR"] == "/var/tmp"
        assert result["PYTEST"] == "/var/tmp"
        # The whole point: never the RAM-backed /tmp.
        assert result["TMPDIR"] != "/tmp"

    def test_honors_override_and_creates_the_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "disk-tmp"
        assert not target.exists()
        result = _run_setup(tmp_path, TEATREE_DISK_TMPDIR=str(target))
        assert result["TMPDIR"] == str(target)
        assert result["PYTEST"] == str(target)
        assert target.is_dir()  # created at boot so mktemp-based tools never break
