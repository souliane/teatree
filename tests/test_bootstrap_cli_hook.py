# test-path: cross-cutting — runs the SessionStart shell hook under a stripped PATH; no src mirror.
"""The SessionStart hook says so when a Docker install cannot keep its MCP server across restarts."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from teatree.docker.workflow import LAUNCHER_MARKER

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "scripts" / "bootstrap-cli.sh"
BASH = shutil.which("bash")
WARNING = "no python3 >= 3.9"

pytestmark = pytest.mark.skipif(BASH is None or shutil.which("grep") is None, reason="needs bash and grep")


def _bin(tmp_path: Path, *, launcher: bool, python: str) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    grep = shutil.which("grep")
    assert grep is not None
    (bin_dir / "grep").symlink_to(grep)
    t3 = bin_dir / "t3"
    body = f'#!/usr/bin/env bash\n{LAUNCHER_MARKER}\nexec /checkout/deploy/t3 "$@"\n' if launcher else "#!/bin/sh\n"
    t3.write_text(body, encoding="utf-8")
    t3.chmod(0o755)
    if python == "current":
        (bin_dir / "python3").symlink_to(sys.executable)
    elif python == "too-old":
        old = bin_dir / "python3"
        old.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        old.chmod(0o755)
    return bin_dir


def _run(bin_dir: Path) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(HOOK)], capture_output=True, text=True, env={"PATH": str(bin_dir)}, timeout=30, check=False
    )


@pytest.mark.parametrize("python", ["absent", "too-old"])
def test_a_docker_launcher_without_a_usable_python_warns_the_session(tmp_path: Path, python: str) -> None:
    completed = _run(_bin(tmp_path, launcher=True, python=python))

    assert completed.returncode == 0
    assert WARNING in completed.stdout
    assert "/mcp" in completed.stdout


def test_a_docker_launcher_with_python_stays_quiet(tmp_path: Path) -> None:
    completed = _run(_bin(tmp_path, launcher=True, python="current"))

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_a_native_t3_needs_no_proxy_and_stays_quiet(tmp_path: Path) -> None:
    completed = _run(_bin(tmp_path, launcher=False, python="absent"))

    assert completed.returncode == 0
    assert completed.stdout == ""


def test_the_python_free_path_really_has_no_python(tmp_path: Path) -> None:
    bin_dir = _bin(tmp_path, launcher=True, python="absent")

    assert shutil.which("python3", path=str(bin_dir)) is None
    assert shutil.which("t3", path=str(bin_dir)) is not None
