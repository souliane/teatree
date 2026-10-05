# test-path: cross-cutting — the prek install in deploy/entrypoint.sh and deploy/Dockerfile (no src mirror).
"""The prek the deploy installs is the one ``uv.lock`` pins, read by ONE helper at install time.

Each install site used to carry its own literal (the Dockerfile 0.3.13, the entrypoint
0.4.10) while the lock had moved to 0.5.x, so the box ran hooks on a prek no CI lane ever
ran. A literal cannot follow the lock, and a lock parser copied into each site can drift
from the others, so every site calls ``deploy/locked-version.sh``.
"""

import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

_TEATREE = Path(__file__).resolve().parents[1]
_DEPLOY = _TEATREE / "deploy"
_HELPER = _DEPLOY / "locked-version.sh"
_BASH = shutil.which("bash") or "/bin/bash"


def _locked(lock: Path, package: str) -> str:
    packages = tomllib.loads(lock.read_text(encoding="utf-8"))["package"]
    return next(entry["version"] for entry in packages if entry["name"] == package)


def _helper(lock: Path, package: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([_BASH, str(_HELPER), str(lock), package], capture_output=True, text=True, check=False)


@pytest.mark.parametrize("script", ["entrypoint.sh", "Dockerfile"])
def test_no_install_site_hard_codes_or_parses_a_version_itself(script: str) -> None:
    body = (_DEPLOY / script).read_text(encoding="utf-8")

    assert not re.findall(r"prek==\d", body)
    assert "gsub(" not in body, "the lock is parsed in deploy/locked-version.sh alone"
    assert "locked-version.sh" in body


def test_the_helper_reads_the_version_the_lock_pins() -> None:
    result = _helper(_TEATREE / "uv.lock", "prek")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _locked(_TEATREE / "uv.lock", "prek")


def test_the_helper_names_nothing_for_a_package_the_lock_does_not_pin() -> None:
    result = _helper(_TEATREE / "uv.lock", "not-a-locked-package")

    assert result.stdout.strip() == ""


def test_the_image_bakes_the_helper_beside_the_entrypoint() -> None:
    dockerfile = (_DEPLOY / "Dockerfile").read_text(encoding="utf-8")

    assert (
        "COPY --chmod=0755 ${TEATREE_CORE_SUBDIR:+${TEATREE_CORE_SUBDIR}/}deploy/locked-version.sh "
        "/usr/local/bin/locked-version.sh"
    ) in dockerfile
    assert dockerfile.index("deploy/locked-version.sh") < dockerfile.index('uv tool install "prek==')
